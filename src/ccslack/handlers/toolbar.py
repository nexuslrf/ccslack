"""Per-provider action toolbar — Block Kit buttons that drive the tmux TUI.

ccgram has a ``/toolbar`` command that renders a 3×3 inline-keyboard grid;
ccslack ports the same idea to Block Kit ``actions`` blocks. Each button
maps to a tmux key sequence (Esc, Tab, arrows, Ctrl-C, digits 1–9, …) sent
to the bound window via ``tmux_manager.send_keys(..., literal=False)``.

Flow:
  * Pinned status message has a ``🎛️ Toolbar`` button.
  * Clicking it posts a new toolbar message in the channel with a few lines
    of live tmux pane text on top of the provider-specific button layout
    (Claude / Codex / Gemini / Pi / Shell).
  * A background refresh task re-captures the pane every
    :data:`REFRESH_INTERVAL` seconds and ``chat.update``s the toolbar when the
    pane content changes, so the live text tracks the TUI while you press
    buttons. The loop runs until the toolbar is closed (or the window dies).
  * Each button click dispatches to ``_on_key`` which sends the named tmux
    key to the bound window. The toolbar message stays put so you can drive
    a picker (arrow keys + Enter) without re-clicking ``🎛️``.
  * A ``Close`` button deletes the toolbar message and cancels its refresh.

Action-id shape:

  * ``ccslack_key:<tmux-key>``    — value carries the ``window_id``.
  * ``ccslack_toolbar_close``     — value carries the message ts to delete.

The tmux key names match ``tmux send-keys`` syntax verbatim — see
https://man7.org/linux/man-pages/man1/tmux.1.html#KEY_BINDINGS.
"""

from __future__ import annotations

import asyncio
import structlog
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from slack_sdk.errors import SlackApiError

from ..session import session_manager
from ..thread_router import thread_router
from ..tmux_manager import tmux_manager
from ..utils import task_done_callback

if TYPE_CHECKING:
    from slack_bolt.async_app import AsyncApp

    from ..slack_client import SlackClient

logger = structlog.get_logger()

# Live-text refresh tunables. The toolbar shows the tail of the tmux pane and
# re-edits the message whenever the pane content changes, until it is closed.
REFRESH_INTERVAL = 1.0  # seconds between pane re-captures
_PANE_SNAPSHOT_LINES = 12  # tail lines shown above the buttons


# (display, tmux_key) — display goes on the button, tmux_key into send-keys.
_Layout = list[list[tuple[str, str]]]


_CLAUDE_LAYOUT: _Layout = [
    [
        (":arrow_left_hook: Esc", "Escape"),
        (":left_right_arrow: Mode", "BTab"),  # Shift+Tab cycles approval mode
        (":bulb: Think", "Tab"),
        (":octagonal_sign: ^C", "C-c"),
    ],
    [
        (":arrow_up: Up", "Up"),
        (":arrow_down: Down", "Down"),
        (":leftwards_arrow_with_hook: Enter", "Enter"),
        (":back: Bksp", "BSpace"),
    ],
    [
        ("1", "1"),
        ("2", "2"),
        ("3", "3"),
        ("4", "4"),
        ("5", "5"),
    ],
]

_CODEX_LAYOUT: _Layout = [
    [
        (":arrow_left_hook: Esc", "Escape"),
        (":arrow_right_hook: Tab", "Tab"),
        (":left_right_arrow: Model", "BTab"),
        (":octagonal_sign: ^C", "C-c"),
    ],
    [
        (":arrow_up: Up", "Up"),
        (":arrow_down: Down", "Down"),
        (":leftwards_arrow_with_hook: Enter", "Enter"),
        (":back: Bksp", "BSpace"),
    ],
    [
        ("1", "1"),
        ("2", "2"),
        ("3", "3"),
        ("4", "4"),
        ("5", "5"),
    ],
]

_GEMINI_LAYOUT: _Layout = _CODEX_LAYOUT
_PI_LAYOUT: _Layout = _CLAUDE_LAYOUT
# cursor-agent is a full-screen TUI driven by arrow keys / Enter / Esc.
_CURSOR_LAYOUT: _Layout = _CLAUDE_LAYOUT

_SHELL_LAYOUT: _Layout = [
    [
        (":leftwards_arrow_with_hook: Enter", "Enter"),
        (":octagonal_sign: ^C", "C-c"),
        (":eject: ^D EOF", "C-d"),
        (":zzz: ^Z Susp", "C-z"),
    ],
    [
        (":arrow_up: Up", "Up"),
        (":arrow_down: Down", "Down"),
        (":arrow_right_hook: Tab", "Tab"),
        (":arrow_left_hook: Esc", "Escape"),
    ],
]


_LAYOUTS: dict[str, _Layout] = {
    "claude": _CLAUDE_LAYOUT,
    "codex": _CODEX_LAYOUT,
    "gemini": _GEMINI_LAYOUT,
    "pi": _PI_LAYOUT,
    "shell": _SHELL_LAYOUT,
    "cursor": _CURSOR_LAYOUT,
}


def _layout_for(provider: str) -> _Layout:
    return _LAYOUTS.get(provider.lower(), _CLAUDE_LAYOUT)


# --------------------------------------------------------------------- live text


async def _capture_pane_snippet(window_id: str) -> str:
    """Last :data:`_PANE_SNAPSHOT_LINES` of cleaned tmux pane text."""
    # Lazy: reuse the interactive picker's ANSI-stripping pane reader so the
    # two live views stay byte-for-byte consistent.
    from .interactive import _capture_pane_snippet as _full_snippet

    snippet = await _full_snippet(window_id)
    if not snippet:
        return ""
    lines = snippet.splitlines()
    return "\n".join(lines[-_PANE_SNAPSHOT_LINES:]) if lines else ""


def _hash_pane(text: str) -> str:
    from .interactive import _hash_pane as _h

    return _h(text)


@dataclass
class _ToolbarSession:
    """Per-message live-toolbar state."""

    channel_id: str
    window_id: str
    message_ts: str
    last_pane_hash: str = ""
    refresh_task: asyncio.Task[None] | None = field(default=None, repr=False)


# Keyed by message ts — a channel can hold more than one open toolbar.
_active_toolbars: dict[str, _ToolbarSession] = {}


def build_toolbar_blocks(
    window_id: str, pane: str | None = None
) -> tuple[list[dict[str, Any]], str]:
    """Build the toolbar's Block Kit blocks for a window. Returns (blocks, fallback).

    When *pane* is provided, its (ANSI-stripped) tail is rendered as a code
    block above the buttons so the toolbar carries a live view of the TUI.
    """
    view = session_manager.view_window(window_id)
    provider = (view.provider_name if view else "") or "claude"
    layout = _layout_for(provider)
    display = thread_router.get_display_name(window_id)

    blocks: list[dict[str, Any]] = [
        {
            "type": "context",
            "elements": [
                {
                    "type": "mrkdwn",
                    "text": (
                        f":control_knobs: *{provider}* toolbar · `{window_id}` "
                        f"({display}) — buttons send keys to the tmux pane."
                    ),
                }
            ],
        }
    ]
    if pane and pane.strip():
        snippet = pane.strip()[:2900]
        blocks.append(
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": f"```\n{snippet}\n```"},
            }
        )
    for row in layout:
        blocks.append(
            {
                "type": "actions",
                "elements": [
                    {
                        "type": "button",
                        "action_id": f"ccslack_key:{tmux_key}",
                        "text": {"type": "plain_text", "text": label},
                        "value": window_id,
                    }
                    for label, tmux_key in row
                ],
            }
        )
    # Close row (separate so it isn't crammed with key buttons).
    blocks.append(
        {
            "type": "actions",
            "elements": [
                {
                    "type": "button",
                    "action_id": "ccslack_toolbar_close",
                    "style": "danger",
                    "text": {"type": "plain_text", "text": ":x: Close"},
                    "value": window_id,
                }
            ],
        }
    )
    fallback = f"{provider} toolbar for {window_id}"
    return blocks, fallback


async def open_toolbar(
    client,  # noqa: ANN001 — Bolt-provided AsyncWebClient
    channel_id: str,
    window_id: str,
) -> str | None:
    """Post the toolbar message in the channel and start its live-text refresh.

    Returns the new message ts (or None on post failure).
    """
    pane = await _capture_pane_snippet(window_id)
    blocks, fallback = build_toolbar_blocks(window_id, pane)
    try:
        result = await client.chat_postMessage(
            channel=channel_id, text=fallback, blocks=blocks
        )
    except SlackApiError as exc:
        logger.warning(
            "toolbar post failed: %s",
            exc.response.get("error") if exc.response else exc,
        )
        return None
    ts = result.get("ts") if hasattr(result, "get") else result["ts"]
    if not ts:
        return None

    session = _ToolbarSession(
        channel_id=channel_id,
        window_id=window_id,
        message_ts=ts,
        last_pane_hash=_hash_pane(pane),
    )
    _active_toolbars[ts] = session
    session.refresh_task = asyncio.create_task(
        _refresh_loop(client, ts), name=f"ccslack-toolbar-refresh-{ts}"
    )
    session.refresh_task.add_done_callback(task_done_callback)
    return ts


async def _refresh_loop(client: SlackClient, ts: str) -> None:
    """Re-edit the toolbar as the pane changes; stop when closed or window dies."""
    while True:
        try:
            await asyncio.sleep(REFRESH_INTERVAL)
        except asyncio.CancelledError:
            raise
        session = _active_toolbars.get(ts)
        if session is None:
            return

        # Window gone between ticks → nothing left to mirror; stop spinning.
        live_window = await tmux_manager.find_window_by_id(session.window_id)
        if live_window is None:
            _active_toolbars.pop(ts, None)
            return

        pane = await _capture_pane_snippet(session.window_id)
        digest = _hash_pane(pane)
        if digest == session.last_pane_hash:
            continue
        session.last_pane_hash = digest
        blocks, fallback = build_toolbar_blocks(session.window_id, pane)
        try:
            await client.chat_update(
                channel=session.channel_id, ts=ts, text=fallback, blocks=blocks
            )
        except SlackApiError as exc:
            error = exc.response.get("error") if exc.response else str(exc)
            if error == "message_not_found":
                # User deleted the toolbar — drop the session and stop.
                _active_toolbars.pop(ts, None)
                return
            logger.debug("toolbar chat.update failed (%s)", error)


def _stop_refresh(ts: str) -> None:
    """Cancel and forget the refresh task for a toolbar message, if any."""
    session = _active_toolbars.pop(ts, None)
    if session is None:
        return
    task = session.refresh_task
    if task is not None and not task.done():
        task.cancel()


def register(app: AsyncApp) -> None:
    """Wire toolbar handlers: open from status bar, single-key dispatch, close."""

    @app.action("ccslack_toolbar_open")
    async def on_open(ack, body, client) -> None:  # noqa: ANN001
        await ack()
        user_id = body.get("user", {}).get("id", "")
        channel_id = body.get("channel", {}).get("id", "")
        # Lazy: auth helper at call site.
        from .auth import is_authorized

        if not is_authorized(user_id, channel_id) or not channel_id:
            return
        # Prefer the channel's live binding — a restore may have rebound this
        # channel to a new window since this button was posted.
        window_id = thread_router.effective_window_id(
            channel_id, _extract_window_id(body, "ccslack_toolbar_open")
        )
        if not window_id:
            return
        await open_toolbar(client, channel_id, window_id)

    @app.action("ccslack_toolbar_close")
    async def on_close(ack, body, client) -> None:  # noqa: ANN001
        await ack()
        user_id = body.get("user", {}).get("id", "")
        channel_id = body.get("channel", {}).get("id", "")
        from .auth import is_authorized

        if not is_authorized(user_id, channel_id) or not channel_id:
            logger.info(
                "toolbar close: auth rejected (user=%s channel=%s)",
                user_id,
                channel_id,
            )
            return
        ts = (body.get("message") or {}).get("ts")
        if not ts:
            logger.warning("toolbar close: no message ts in action body")
            return
        # Stop the live-text refresh before removing the message so the loop
        # never races a chat.update against the delete.
        _stop_refresh(ts)
        # Lazy: shared close helper lives in slack_sender; pulling at call
        # site keeps the toolbar import graph thin.
        from ..slack_sender import safe_close_message

        await safe_close_message(client, channel=channel_id, ts=ts, label="toolbar")

    # One handler matches every ccslack_key:<...> action_id. Bolt's matcher
    # supports regex; we use a prefix predicate so each key only needs the
    # ``ccslack_key:<name>`` value in the layout above.
    import re as _re

    @app.action(_re.compile(r"^ccslack_key:.+$"))
    async def on_key(ack, body, _client) -> None:  # noqa: ANN001
        await ack()
        await _dispatch_key(body)


async def _dispatch_key(body: dict[str, Any]) -> None:
    """Resolve action_id → tmux key → send_keys to the bound window."""
    user_id = body.get("user", {}).get("id", "")
    channel_id = body.get("channel", {}).get("id", "")
    from .auth import is_authorized

    if not is_authorized(user_id, channel_id):
        return

    action_id = ""
    button_value = ""
    for action in body.get("actions", []) or []:
        aid = action.get("action_id", "")
        if aid.startswith("ccslack_key:"):
            action_id = aid
            button_value = action.get("value", "")
            break
    # Prefer the channel's live binding — a restore may have rebound this
    # channel to a new window since this toolbar was posted.
    window_id = thread_router.effective_window_id(channel_id, button_value)
    if not action_id or not window_id:
        return

    tmux_key = action_id.split(":", 1)[1]
    if not tmux_key:
        return

    try:
        await tmux_manager.send_keys(window_id, tmux_key, literal=False, enter=False)
    except OSError, RuntimeError:
        logger.exception("toolbar send_keys(%s, %s) failed", window_id, tmux_key)


def _extract_window_id(body: dict[str, Any], action_id: str) -> str:
    for action in body.get("actions", []) or []:
        if action.get("action_id") == action_id:
            return action.get("value", "")
    return ""


__all__ = [
    "REFRESH_INTERVAL",
    "build_toolbar_blocks",
    "open_toolbar",
    "register",
]
