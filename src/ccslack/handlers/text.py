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

from ..config import config
from ..slack_client import BoltSlackClient
from ..thread_router import thread_router
from ..tmux_manager import tmux_manager
from . import shell_capture

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
        text = event.get("text", "")

        if not channel_id or not user_id:
            return

        # Skip messages in unbound channels (incl. the meta channel).
        window_id = thread_router.get_window_for_channel(channel_id)
        if window_id is None:
            return

        if not config.is_user_allowed(user_id):
            await _react(client, channel_id, event.get("ts", ""), "no_entry_sign")
            return

        if not text.strip():
            return

        # Shell sessions have no transcript — snapshot the pane *before* the
        # keypress so the post-send capture can diff against it. Pass the
        # command along so the capture can prefix the post with ``> <cmd>``
        # and strip the terminal echo of the same line.
        is_shell = shell_capture.is_shell_window(window_id)
        if is_shell:
            await shell_capture.snapshot_pre_send(window_id, command=text)

        try:
            await tmux_manager.send_keys(window_id, text)
        except OSError, RuntimeError:
            logger.exception("send_keys failed for window %s", window_id)
            await _react(client, channel_id, event.get("ts", ""), "warning")
            return

        await _react(client, channel_id, event.get("ts", ""), "white_check_mark")

        if is_shell:
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
