"""Pinned status-message lifecycle + status-bar action handlers for session channels.

Each bound Slack channel has one pinned status message that ccslack ``chat.update``s
on state transitions. Replaces ccgram's topic-emoji recoloring (Slack rate-limits
channel renames, so a pinned, editable message is the natural fit).

States and their rendered headers (``CCSLACK_STATUS_MODE`` selects the colour mode):

  * ``active`` — agent working
  * ``idle``   — agent waiting for user input
  * ``done``   — Stop hook just fired
  * ``dead``   — tmux window gone (recovery banner takes over)

Public API:

  * ``ensure_status_message(client, channel_id, window_id)`` — idempotent;
    posts + pins the initial message if not already present.
  * ``update_status(client, channel_id, window_id, state, **kw)`` — flips the
    state label and edits the message in place. Safe to call repeatedly; the
    last_state cache short-circuits no-op updates.
  * ``clear_status_message(client, channel_id, window_id)`` — best-effort
    cleanup when archiving a session.
"""

from __future__ import annotations

import asyncio
import contextlib
import structlog
import time
from typing import TYPE_CHECKING, Any

from slack_sdk.errors import SlackApiError

from ..config import config
from ..session import session_manager
from ..thread_router import thread_router
from ..window_state_store import window_store

if TYPE_CHECKING:
    from ..slack_client import SlackClient

logger = structlog.get_logger()


def _prune_if_channel_gone(channel_id: str, window_id: str, error: str) -> bool:
    """Prune the binding when a post failed because the channel is gone.

    Returns True when the channel was pruned (caller should stop). A deleted /
    archived Slack channel would otherwise make the poll loop retry — and log
    ``channel_not_found`` — every tick forever.
    """
    # Lazy: coordinator pulls router/store; import at the error path only.
    from .polling.coordinator import is_channel_gone, prune_channel

    if not is_channel_gone(error):
        return False
    prune_channel(channel_id, window_id)
    return True


# Per-channel debounce for status updates. Slack tier-3 limits are 1/sec per
# channel; we keep a 0.8s minimum gap so the queue never starves the rest of
# the channel's traffic.
_DEBOUNCE_SECONDS = 0.8
_last_update: dict[str, float] = {}

# Status label → header emoji (system colour mode: green = working).
_HEADER_EMOJI_SYSTEM = {
    "active": ":large_green_circle:",
    "idle": ":large_yellow_circle:",
    "done": ":white_check_mark:",
    "dead": ":x:",
}
# user colour mode: green = ready for me.
_HEADER_EMOJI_USER = {
    "active": ":large_yellow_circle:",
    "idle": ":large_green_circle:",
    "done": ":white_check_mark:",
    "dead": ":x:",
}
_HEADER_LABEL = {
    "active": "working",
    "idle": "idle — ready for input",
    "done": "done",
    "dead": "dead — window gone",
}


def _emoji_for(state: str) -> str:
    table = _HEADER_EMOJI_USER if config.status_mode == "user" else _HEADER_EMOJI_SYSTEM
    return table.get(state, ":grey_question:")


def _build_blocks(
    window_id: str,
    state: str,
    *,
    detail: str | None = None,
) -> tuple[list[dict[str, Any]], str]:
    """Build (blocks, fallback_text) for the status message."""
    view = session_manager.view_window(window_id)
    provider = (view.provider_name if view else "") or "?"
    cwd = (view.cwd if view else "") or "?"
    display = thread_router.get_display_name(window_id)
    emoji = _emoji_for(state)
    label = _HEADER_LABEL.get(state, state)
    header_text = f"{emoji} *{label}*  ·  `{provider}`  ·  `{window_id}`"

    blocks: list[dict[str, Any]] = [
        {"type": "section", "text": {"type": "mrkdwn", "text": header_text}},
        {
            "type": "context",
            "elements": [
                {"type": "mrkdwn", "text": f":file_folder: `{cwd}`"},
                {"type": "mrkdwn", "text": f":label: `{display}`"},
            ],
        },
    ]
    if detail:
        blocks.append(
            {
                "type": "context",
                "elements": [{"type": "mrkdwn", "text": detail}],
            }
        )
    blocks.append(
        {
            "type": "actions",
            "block_id": f"ccslack_status_actions:{window_id}",
            "elements": [
                {
                    "type": "button",
                    "action_id": "ccslack_screenshot",
                    "text": {"type": "plain_text", "text": ":camera: Screenshot"},
                    "value": window_id,
                },
                {
                    "type": "button",
                    "action_id": "ccslack_toolbar_open",
                    "text": {"type": "plain_text", "text": ":control_knobs: Toolbar"},
                    "value": window_id,
                },
                {
                    "type": "button",
                    "action_id": "ccslack_send_open",
                    "text": {"type": "plain_text", "text": ":outbox_tray: File"},
                    "value": window_id,
                },
                {
                    "type": "button",
                    "action_id": "ccslack_archive",
                    "style": "danger",
                    "text": {"type": "plain_text", "text": ":wastebasket: Archive"},
                    "value": window_id,
                    "confirm": {
                        "title": {"type": "plain_text", "text": "Archive session?"},
                        "text": {
                            "type": "mrkdwn",
                            "text": (
                                f"This archives the Slack channel and kills tmux window `{window_id}`."
                            ),
                        },
                        "confirm": {"type": "plain_text", "text": "Archive"},
                        "deny": {"type": "plain_text", "text": "Cancel"},
                    },
                },
            ],
        }
    )
    fallback = f"{label} · {provider} · {window_id} · {cwd}"
    return blocks, fallback


async def ensure_status_message(
    client: SlackClient,
    channel_id: str,
    window_id: str,
    *,
    initial_state: str = "idle",
) -> str | None:
    """Idempotently post + pin the channel's status message.

    Returns the message ``ts`` (existing or newly posted). On API failure logs
    and returns None — callers should treat that as "no status message yet".
    """
    state = window_store.get_window_state(window_id)
    if state.status_message_ts:
        return state.status_message_ts

    blocks, fallback = _build_blocks(window_id, initial_state)
    try:
        result = await client.chat_postMessage(
            channel=channel_id, text=fallback, blocks=blocks
        )
        ts = result.get("ts") if hasattr(result, "get") else result["ts"]
    except SlackApiError as exc:
        error = exc.response.get("error") if exc.response else str(exc)
        if _prune_if_channel_gone(channel_id, window_id, error):
            return None
        logger.warning("ensure_status_message: chat.postMessage failed: %s", error)
        return None

    state.status_message_ts = ts
    state.status_state = initial_state
    session_manager._save_state()

    try:
        await client.pins_add(channel=channel_id, timestamp=ts)
    except SlackApiError as exc:
        # already_pinned is fine; other failures are best-effort.
        error = exc.response.get("error") if exc.response else str(exc)
        if error not in ("already_pinned", None):
            logger.debug("pins.add for status message failed: %s", error)
    return ts


async def update_status(
    client: SlackClient,
    channel_id: str,
    window_id: str,
    state_label: str,
    *,
    detail: str | None = None,
) -> bool:
    """Edit the pinned status message to reflect a new state.

    Idempotent: when the cached state matches the requested label and the
    last update was inside the debounce window, the call is a no-op.
    """
    state = window_store.window_states.get(window_id)
    if state is None:
        return False

    ts = state.status_message_ts
    if not ts:
        ts = await ensure_status_message(
            client, channel_id, window_id, initial_state=state_label
        )
        if not ts:
            return False

    now = time.monotonic()
    last = _last_update.get(channel_id, 0.0)
    if (
        state.status_state == state_label
        and detail is None
        and now - last < _DEBOUNCE_SECONDS
    ):
        return False
    if now - last < _DEBOUNCE_SECONDS:
        await asyncio.sleep(_DEBOUNCE_SECONDS - (now - last))

    blocks, fallback = _build_blocks(window_id, state_label, detail=detail)
    try:
        await client.chat_update(
            channel=channel_id, ts=ts, text=fallback, blocks=blocks
        )
    except SlackApiError as exc:
        error = exc.response.get("error") if exc.response else str(exc)
        if error == "message_not_found":
            # User deleted it. Forget the ts so the next call re-posts.
            state.status_message_ts = ""
            session_manager._save_state()
            return False
        if _prune_if_channel_gone(channel_id, window_id, error):
            return False
        logger.warning("chat.update for status message failed: %s", error)
        return False

    state.status_state = state_label
    session_manager._save_state()
    _last_update[channel_id] = time.monotonic()
    return True


async def clear_status_message(
    client: SlackClient,
    channel_id: str,
    window_id: str,
) -> None:
    """Unpin and delete the status message (best-effort)."""
    state = window_store.window_states.get(window_id)
    if state is None or not state.status_message_ts:
        return
    ts = state.status_message_ts
    with contextlib.suppress(SlackApiError):
        await client.pins_remove(channel=channel_id, timestamp=ts)
    with contextlib.suppress(SlackApiError):
        await client.chat_delete(channel=channel_id, ts=ts)
    state.status_message_ts = ""
    session_manager._save_state()


def register(app) -> None:  # noqa: ANN001
    """Wire status-bar action button handlers (Archive, etc.)."""

    @app.action("ccslack_archive")
    async def on_archive_click(ack, body, client) -> None:  # noqa: ANN001
        await ack()
        user_id = body.get("user", {}).get("id", "")
        channel_id = body.get("channel", {}).get("id", "")
        # Lazy: auth helper imported at call site.
        from .auth import is_authorized

        if not is_authorized(user_id, channel_id):
            return
        button_value = ""
        for action in body.get("actions", []) or []:
            if action.get("action_id") == "ccslack_archive":
                button_value = action.get("value", "")
                break
        # Prefer the channel's live binding — a restore may have rebound this
        # channel to a new window, and archive must target the current one.
        window_id = thread_router.effective_window_id(channel_id, button_value)
        if not window_id or not channel_id:
            return
        # Lazy: kill_window pulls libtmux at call time; same module
        # already imported at top, but the import is harmless here.
        from ..tmux_manager import tmux_manager as _tm

        try:
            await _tm.kill_window(window_id)
        except OSError, RuntimeError:
            logger.exception("kill_window failed for %s", window_id)

        thread_router.unbind_channel(channel_id)
        # Drop the WindowState entirely — archive is destructive intent.
        window_store.remove_window(window_id)

        # Drop any open tool-call thread state for the channel.
        from .messaging_pipeline.turn_threads import clear_channel

        clear_channel(channel_id)
        thread_router.clear_chat_threads(channel_id)
        thread_router.clear_channel_grants(channel_id)
        from .purge import forget_channel as _purge_forget
        _purge_forget(channel_id)

        # Best-effort: archive the channel.
        try:
            await client.conversations_archive(channel=channel_id)
        except SlackApiError as exc:
            logger.debug(
                "conversations.archive failed: %s",
                exc.response.get("error") if exc.response else exc,
            )


__all__ = [
    "clear_status_message",
    "ensure_status_message",
    "register",
    "update_status",
]
