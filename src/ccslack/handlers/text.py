"""Inbound text handler — Slack message → tmux ``send-keys``.

Walking-skeleton routing:

  * Bot messages and message-subtype edits / joins / leaves are ignored.
  * Messages in unbound channels (including the meta channel) are ignored —
    use ``/ccslack`` slash commands there instead.
  * Messages in bound session channels are forwarded verbatim to the bound
    tmux window via ``tmux_manager.send_keys``.

Authorization: the message author must be in ``ALLOWED_USERS``. A reaction
(``:no_entry_sign:``) is added to messages from unauthorized users so they
get visible feedback without us spamming the channel.
"""

from __future__ import annotations

import structlog
from typing import TYPE_CHECKING

from slack_sdk.errors import SlackApiError

from ..slack_client import BoltSlackClient
from ..slack_inbound import decode_slack_text
from ..thread_router import thread_router
from ..tmux_manager import tmux_manager
from . import shell_capture, shell_marker
from .auth import is_authorized

if TYPE_CHECKING:
    from slack_bolt.async_app import AsyncApp

logger = structlog.get_logger()


def register(app: AsyncApp) -> None:
    """Wire the inbound ``message`` event handler."""

    @app.event("message")
    async def on_message(event: dict, client) -> None:  # noqa: ANN001
        # Ignore bot messages, subtype events (edits, joins, …), thread broadcasts.
        if event.get("bot_id") or event.get("subtype"):
            return

        channel_id = event.get("channel", "")
        user_id = event.get("user", "")
        # Slack encodes inbound text: it wraps auto-linked URLs and mentions in
        # <…> and escapes literal &,<,> as HTML entities. Decode before we type
        # it into tmux, or e.g. `git clone <https://…>` reaches bash with the
        # angle brackets intact and fails as a redirection.
        text = decode_slack_text(event.get("text", ""))

        if not channel_id or not user_id:
            return

        # Skip messages in unbound channels (incl. the meta channel).
        window_id = thread_router.get_window_for_channel(channel_id)
        if window_id is None:
            return

        # Replies inside a human-only "chat" thread (`/ccslack chat`) are a
        # side conversation — never forwarded to the agent.
        thread_ts = event.get("thread_ts", "")
        if thread_ts and thread_router.is_chat_thread(channel_id, thread_ts):
            return

        if not is_authorized(user_id, channel_id):
            await _react(client, channel_id, event.get("ts", ""), "no_entry_sign")
            return

        if not text.strip():
            return

        # Two shell paths in priority order:
        #   1. Marker-driven (preferred) — when the ⌘N⌘ prompt marker is
        #      present, the passive monitor in the polling loop picks up
        #      output as it streams. We just record the user's Slack ts
        #      so the eventual exit code can land as a ✅/❌ reaction.
        #   2. Pane-diff fallback — no marker (setup never ran, ``exec
        #      bash`` blew it away, exotic shell). Use the original
        #      pre/post snapshot diff.
        is_shell = shell_capture.is_shell_window(window_id)
        marker_active = False
        if is_shell:
            marker_active = await shell_marker.has_marker(window_id)
            if marker_active:
                shell_marker.mark_slack_command(
                    window_id, slack_user_message_ts=event.get("ts", "")
                )
            else:
                await shell_capture.snapshot_pre_send(window_id, command=text)

        try:
            await tmux_manager.send_keys(window_id, text)
        except OSError, RuntimeError:
            logger.exception("send_keys failed for window %s", window_id)
            await _react(client, channel_id, event.get("ts", ""), "warning")
            return

        # ✓ reaction confirms the keypress reached tmux. On the marker path,
        # this will be replaced by ✅ / ❌ from ``shell_marker`` once the
        # command finishes and the exit code lands.
        await _react(client, channel_id, event.get("ts", ""), "white_check_mark")

        if is_shell and not marker_active:
            shell_capture.schedule_capture(
                BoltSlackClient(client), channel_id, window_id
            )


async def _react(
    client,  # noqa: ANN001
    channel: str,
    timestamp: str,
    name: str,
) -> None:
    """Add an emoji reaction (best-effort)."""
    if not timestamp:
        return
    try:
        await client.reactions_add(channel=channel, name=name, timestamp=timestamp)
    except SlackApiError as exc:
        error = exc.response.get("error") if exc.response else str(exc)
        if error == "already_reacted":
            return
        logger.debug("reactions.add(%s) failed: %s", name, error)


__all__ = ["register"]
