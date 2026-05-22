"""Live interactive picker — ccgram-style tool_use-driven TUI mirror.

Ports the mechanism behind ``ccgram/handlers/interactive/interactive_ui.py`` to
Slack Block Kit. Detection is driven by the JSONL transcript (a ``tool_use``
of ``AskUserQuestion`` / ``ExitPlanMode`` / ``request_user_input``), not by
regex scraping — that path is instant, exact, and provider-uniform.

Lifecycle:

  1. ``handle_new_message`` in ``messaging_pipeline/message_routing.py`` sees a
     ``tool_use`` whose ``tool_name`` is in :data:`INTERACTIVE_TOOL_NAMES` and
     calls :func:`enter_interactive_mode`. The picker is posted (or the
     existing one re-edited) and a background refresh task starts.
  2. The refresh task captures the pane every ``REFRESH_INTERVAL`` seconds.
     When the pane *content* changes, the picker message is ``chat.update``d
     so users see the picker move as they press buttons.
  3. Picker buttons dispatch to ``ccslack_key:<tmux-key>`` (the toolbar
     handler) so a single key handler covers all arrow / Enter / Esc / digit
     paths. A separate ``ccslack_live_dismiss`` action closes the picker.
  4. Exit conditions, any of:
       * the matching ``tool_result`` arrives — handled by
         :func:`maybe_exit_for_tool_result`;
       * the agent's ``Stop`` hook fires — :func:`exit_for_window`;
       * the tmux window dies — :func:`exit_for_window`;
       * no pane change for :data:`IDLE_EXIT_SECONDS` — refresh task exits;
       * the user clicks ``Dismiss``.

Fallback paths:

  * :func:`handle_notification` — Claude's ``Notification`` hook (permission
    prompts) lacks a ``tool_use`` event in JSONL; it routes through this same
    subsystem with a synthetic, hook-derived trigger.
  * :func:`enter_from_pane` — the polling-loop ``prompt_probe`` (heuristic
    regex) triggers this when a non-hook-driven provider has a prompt up but
    no ``tool_use`` was detected (e.g. Codex picker that streams *after* the
    tool_use). Same picker, same refresh, same exits.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import re
import structlog
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from slack_sdk.errors import SlackApiError

from ..config import config
from ..providers.base import HookEvent
from ..thread_router import thread_router
from ..tmux_manager import tmux_manager
from ..utils import task_done_callback

if TYPE_CHECKING:
    from slack_bolt.async_app import AsyncApp

    from ..slack_client import SlackClient

logger = structlog.get_logger()

# Tool names that always trigger live interactive mode. Sourced from ccgram's
# ``INTERACTIVE_TOOL_NAMES`` for parity.
INTERACTIVE_TOOL_NAMES: frozenset[str] = frozenset(
    {
        "AskUserQuestion",  # Claude
        "ExitPlanMode",  # Claude
        "request_user_input",  # Codex
    }
)

# Pane capture / refresh tunables.
_PANE_SNAPSHOT_LINES = 30
REFRESH_INTERVAL = 0.8  # seconds between pane re-captures while in the picker
IDLE_EXIT_SECONDS = 60.0  # pane unchanged for this long → auto-exit
HASH_LEN = 16

# ANSI escape stripper — pane comes in colour-encoded for the screenshot path;
# we drop codes for the code-block view.
_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]|\x1b\][^\x07]*\x07")


def _strip_ansi(text: str) -> str:
    return _ANSI_RE.sub("", text)


def _hash_pane(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8"), usedforsecurity=False).hexdigest()[
        :HASH_LEN
    ]


def _result_ts(result: Any) -> str:
    """Pull the ``ts`` out of a chat.postMessage response.

    Tolerates the slack_sdk ``AsyncSlackResponse`` (dict-like ``.get``), plain
    dicts, and test fakes that omit ``ts`` entirely (returns empty string).
    """
    if result is None:
        return ""
    if hasattr(result, "get"):
        return str(result.get("ts") or "")
    try:
        return str(result["ts"])
    except KeyError, IndexError, TypeError:
        return ""


async def _capture_pane_snippet(window_id: str) -> str:
    """Last ``_PANE_SNAPSHOT_LINES`` of cleaned pane text."""
    raw = await tmux_manager.capture_pane_scrollback(
        window_id, history=200, with_ansi=True
    )
    if not raw:
        return ""
    cleaned = _strip_ansi(raw).rstrip()
    lines = cleaned.splitlines()
    return "\n".join(lines[-_PANE_SNAPSHOT_LINES:]) if lines else ""


# --------------------------------------------------------------------- state


@dataclass
class InteractiveSession:
    """Per-channel live picker state."""

    channel_id: str
    window_id: str
    message_ts: str
    tool_use_id: str | None
    tool_name: str
    started_at: float
    last_change_at: float
    last_pane_hash: str = ""
    refresh_task: asyncio.Task[None] | None = field(default=None, repr=False)


# Keyed by channel_id — each Slack channel has at most one active picker.
_active: dict[str, InteractiveSession] = {}


def is_in_interactive_mode(channel_id: str) -> bool:
    """True iff this channel has a live picker."""
    return channel_id in _active


def session_for_window(window_id: str) -> InteractiveSession | None:
    """Look up the session bound to ``window_id``, if any."""
    for session in _active.values():
        if session.window_id == window_id:
            return session
    return None


# --------------------------------------------------------------------- blocks


def _build_blocks(
    *,
    window_id: str,
    tool_name: str,
    pane: str,
    started_at: float,
) -> tuple[list[dict[str, Any]], str]:
    """Build the live picker's Block Kit payload.

    Always renders the active picker. The resolved/terminal state is handled by
    ``_close_session`` deleting the message outright (matching ccgram).
    """
    display = thread_router.get_display_name(window_id)
    elapsed = max(0, int(time.monotonic() - started_at))
    headline = ":bell: *Interactive prompt — live picker.*"
    detail = " · ".join(
        [
            f"`{tool_name or 'prompt'}`",
            f"`{window_id}` ({display})",
            f"{elapsed}s elapsed",
        ]
    )

    blocks: list[dict[str, Any]] = [
        {"type": "section", "text": {"type": "mrkdwn", "text": headline}},
        {"type": "context", "elements": [{"type": "mrkdwn", "text": detail}]},
    ]
    if pane.strip():
        snippet = pane.strip()[:2900]
        blocks.append(
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": f"```\n{snippet}\n```"},
            }
        )
    blocks.extend(_button_rows(window_id))
    fallback = f"Interactive prompt · {window_id}"
    return blocks, fallback


def _button_rows(window_id: str) -> list[dict[str, Any]]:
    """Three action rows: arrows / control keys / digits + dismiss.

    Arrow/control buttons route through ``ccslack_key:<tmux>`` (the same
    handler the toolbar uses). Dismiss has its own action so the picker can
    close locally without sending keys.
    """

    def key(emoji: str, tmux: str) -> dict[str, Any]:
        return {
            "type": "button",
            "action_id": f"ccslack_key:{tmux}",
            "text": {"type": "plain_text", "text": emoji},
            "value": window_id,
        }

    arrows = {
        "type": "actions",
        "elements": [
            key(":arrow_up: Up", "Up"),
            key(":arrow_down: Down", "Down"),
            key(":arrow_left: Left", "Left"),
            key(":arrow_right: Right", "Right"),
        ],
    }
    controls = {
        "type": "actions",
        "elements": [
            {
                "type": "button",
                "action_id": "ccslack_key:Enter",
                "style": "primary",
                "text": {
                    "type": "plain_text",
                    "text": ":leftwards_arrow_with_hook: Enter",
                },
                "value": window_id,
            },
            key(":arrow_left_hook: Esc", "Escape"),
            key(":arrow_right_hook: Tab", "Tab"),
            key(":blank: Space", "Space"),
            key(":back: Bksp", "BSpace"),
        ],
    }
    digits = {
        "type": "actions",
        "elements": [key(d, d) for d in ("1", "2", "3", "4", "5")],
    }
    dismiss = {
        "type": "actions",
        "elements": [
            {
                "type": "button",
                "action_id": "ccslack_live_dismiss",
                "style": "danger",
                "text": {"type": "plain_text", "text": ":wastebasket: Dismiss"},
                "value": window_id,
            }
        ],
    }
    return [arrows, controls, digits, dismiss]


# --------------------------------------------------------------------- enter


async def enter_interactive_mode(
    client: SlackClient,
    *,
    channel_id: str,
    window_id: str,
    tool_use_id: str | None,
    tool_name: str,
) -> None:
    """Open or refresh a live picker for ``channel_id``.

    Idempotent — if a session is already live for this channel, the existing
    message is re-edited rather than a new one being posted. Starts the
    background refresh task on first entry.
    """
    pane = await _capture_pane_snippet(window_id)
    digest = _hash_pane(pane)
    now = time.monotonic()

    existing = _active.get(channel_id)
    if existing is not None and existing.window_id == window_id:
        # Re-trigger for the same window — just update tool metadata, refresh
        # the message, and keep the refresh loop running.
        existing.tool_use_id = tool_use_id or existing.tool_use_id
        existing.tool_name = tool_name or existing.tool_name
        if digest != existing.last_pane_hash:
            existing.last_pane_hash = digest
            existing.last_change_at = now
            await _safe_update(
                client,
                channel_id=channel_id,
                ts=existing.message_ts,
                pane=pane,
                tool_name=existing.tool_name,
                started_at=existing.started_at,
            )
        return

    # Different window claimed the channel, or no session — start fresh.
    if existing is not None:
        await _close_session(client, existing, reason="superseded")

    blocks, fallback = _build_blocks(
        window_id=window_id, tool_name=tool_name, pane=pane, started_at=now
    )
    try:
        result = await client.chat_postMessage(
            channel=channel_id, text=fallback, blocks=blocks
        )
    except SlackApiError as exc:
        logger.warning(
            "interactive: chat.postMessage failed: %s",
            exc.response.get("error") if exc.response else exc,
        )
        return
    message_ts = _result_ts(result)
    if not message_ts:
        logger.debug("interactive: chat.postMessage returned no ts; aborting")
        return

    session = InteractiveSession(
        channel_id=channel_id,
        window_id=window_id,
        message_ts=message_ts,
        tool_use_id=tool_use_id,
        tool_name=tool_name,
        started_at=now,
        last_change_at=now,
        last_pane_hash=digest,
    )
    _active[channel_id] = session

    session.refresh_task = asyncio.create_task(
        _refresh_loop(client, channel_id), name=f"ccslack-int-refresh-{channel_id}"
    )
    session.refresh_task.add_done_callback(task_done_callback)
    logger.info(
        "interactive: entered for window %s in channel %s (tool=%s)",
        window_id,
        channel_id,
        tool_name,
    )


async def enter_from_pane(
    client: SlackClient,
    *,
    channel_id: str,
    window_id: str,
    provider: str,
) -> None:
    """Heuristic-driven entry (pane regex match in non-hook providers)."""
    await enter_interactive_mode(
        client,
        channel_id=channel_id,
        window_id=window_id,
        tool_use_id=None,
        tool_name=provider or "pane-detected",
    )


# --------------------------------------------------------------------- exit


async def exit_interactive_mode(
    client: SlackClient,
    channel_id: str,
    *,
    reason: str = "resolved",
) -> bool:
    """Close the live picker for ``channel_id``. Returns True if one was active."""
    session = _active.pop(channel_id, None)
    if session is None:
        return False
    await _close_session(client, session, reason=reason)
    return True


async def maybe_exit_for_tool_result(
    client: SlackClient,
    tool_use_id: str,
) -> bool:
    """Close the picker whose triggering ``tool_use_id`` matches. Returns True if closed."""
    if not tool_use_id:
        return False
    target_channel: str | None = None
    for channel_id, session in _active.items():
        if session.tool_use_id == tool_use_id:
            target_channel = channel_id
            break
    if target_channel is None:
        return False
    return await exit_interactive_mode(client, target_channel, reason="resolved")


async def exit_for_window(
    client: SlackClient,
    window_id: str,
    *,
    reason: str = "session ended",
) -> bool:
    """Close any picker bound to ``window_id``. Returns True if one was active."""
    target_channel: str | None = None
    for channel_id, session in _active.items():
        if session.window_id == window_id:
            target_channel = channel_id
            break
    if target_channel is None:
        return False
    return await exit_interactive_mode(client, target_channel, reason=reason)


async def _close_session(
    client: SlackClient,
    session: InteractiveSession,
    *,
    reason: str,
) -> None:
    """Cancel refresh task and remove the picker message.

    ccgram's policy (mirrored here): once a picker resolves, delete it. The
    next transcript line from the agent provides the resolution context, and a
    lingering "Interactive prompt resolved" stub with a 30-line pane snapshot
    just bloats the channel with stale terminal state.
    """
    if session.refresh_task is not None and not session.refresh_task.done():
        session.refresh_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await session.refresh_task

    with contextlib.suppress(SlackApiError):
        await client.chat_delete(channel=session.channel_id, ts=session.message_ts)

    logger.info(
        "interactive: closed for window %s in channel %s (reason=%s)",
        session.window_id,
        session.channel_id,
        reason,
    )


# --------------------------------------------------------------------- refresh


async def _refresh_loop(client: SlackClient, channel_id: str) -> None:
    """Re-edit the picker as the pane changes; auto-exit on idle."""
    while True:
        try:
            await asyncio.sleep(REFRESH_INTERVAL)
        except asyncio.CancelledError:
            raise
        session = _active.get(channel_id)
        if session is None:
            return

        # Window might have died between ticks; let the polling coordinator
        # see "dead" and call exit_for_window — we just stop spinning.
        live_window = await tmux_manager.find_window_by_id(session.window_id)
        if live_window is None:
            return

        pane = await _capture_pane_snippet(session.window_id)
        digest = _hash_pane(pane)
        now = time.monotonic()
        if digest != session.last_pane_hash:
            session.last_pane_hash = digest
            session.last_change_at = now
            await _safe_update(
                client,
                channel_id=channel_id,
                ts=session.message_ts,
                pane=pane,
                tool_name=session.tool_name,
                started_at=session.started_at,
            )
        elif now - session.last_change_at > IDLE_EXIT_SECONDS:
            # No pane change for IDLE_EXIT_SECONDS — assume the prompt resolved
            # off-channel (someone drove tmux directly) and close.
            await exit_interactive_mode(client, channel_id, reason="idle timeout")
            return


async def _safe_update(
    client: SlackClient,
    *,
    channel_id: str,
    ts: str,
    pane: str,
    tool_name: str,
    started_at: float,
) -> None:
    """chat.update wrapper that swallows expected Slack errors."""
    window_id = ""
    session = _active.get(channel_id)
    if session is not None:
        window_id = session.window_id
    blocks, fallback = _build_blocks(
        window_id=window_id, tool_name=tool_name, pane=pane, started_at=started_at
    )
    try:
        await client.chat_update(
            channel=channel_id, ts=ts, text=fallback, blocks=blocks
        )
    except SlackApiError as exc:
        error = exc.response.get("error") if exc.response else str(exc)
        if error == "message_not_found":
            # User deleted the picker — drop the session.
            _active.pop(channel_id, None)
            return
        logger.debug("interactive chat.update failed (%s)", error)


# --------------------------------------------------------------------- hooks


async def handle_notification(event: HookEvent, client: SlackClient) -> None:
    """Claude ``Notification`` hook → enter interactive mode for that window."""
    window_id = _strip_session_prefix(event.window_key)
    channel_id = thread_router.get_channel_for_window(window_id)
    if channel_id is None:
        logger.debug("Notification for unbound window %s; ignoring", window_id)
        return
    data = event.data or {}
    tool_name = (
        str(data.get("tool_name") or "")
        or str(data.get("notification_type") or "")
        or "permission"
    )
    await enter_interactive_mode(
        client,
        channel_id=channel_id,
        window_id=window_id,
        tool_use_id=None,
        tool_name=tool_name,
    )


def _strip_session_prefix(window_key: str) -> str:
    """``"ccslack:@12"`` → ``"@12"``. Foreign IDs (emdash) kept verbatim."""
    if ":" not in window_key:
        return window_key
    prefix, rest = window_key.split(":", 1)
    if prefix == config.tmux_session_name and rest.startswith("@"):
        return rest
    return window_key


# --------------------------------------------------------------------- actions


def register(app: AsyncApp) -> None:
    """Wire the picker's Dismiss handler. Key actions reuse ``ccslack_key:*``."""

    @app.action("ccslack_live_dismiss")
    async def on_dismiss(ack, body, client) -> None:  # noqa: ANN001
        await ack()
        user_id = body.get("user", {}).get("id", "")
        channel_id = body.get("channel", {}).get("id", "")
        if not config.is_user_allowed(user_id) or not channel_id:
            return
        await exit_interactive_mode(client, channel_id, reason="dismissed")


__all__ = [
    "INTERACTIVE_TOOL_NAMES",
    "IDLE_EXIT_SECONDS",
    "InteractiveSession",
    "REFRESH_INTERVAL",
    "enter_from_pane",
    "enter_interactive_mode",
    "exit_for_window",
    "exit_interactive_mode",
    "handle_notification",
    "is_in_interactive_mode",
    "maybe_exit_for_tool_result",
    "register",
    "session_for_window",
]
