"""Inbound text handler — Slack message → tmux ``send-keys``.

Walking-skeleton routing:

  * Bot messages and message-subtype edits / joins / leaves are ignored.
  * Messages in unbound channels (including the meta channel) are ignored —
    use ``/ccslack`` slash commands there instead.
  * Messages in bound session channels are forwarded to the bound tmux window
    (via ``agent_input.deliver_to_agent``) — decoded from Slack's link/entity
    encoding first, with the bot @-mention stripped.
  * In ``manual`` input-mode channels (``/ccslack manual on``) a plain message
    stays as human chat and is *not* forwarded; the agent runs only when the
    message @-mentions the bot (or via ``/ccslack run``).

Authorization: the message author must be in ``ALLOWED_USERS``. A reaction
(``:no_entry_sign:``) is added to messages from unauthorized users so they
get visible feedback without us spamming the channel.
"""

from __future__ import annotations

import structlog
from typing import TYPE_CHECKING

from slack_sdk.errors import SlackApiError

from .. import window_query
from ..slack_inbound import decode_slack_text
from ..thread_router import thread_router
from .agent_input import deliver_to_agent
from .auth import is_authorized

if TYPE_CHECKING:
    from slack_bolt.async_app import AsyncApp

logger = structlog.get_logger()


def register(app: AsyncApp) -> None:
    """Wire the inbound ``message`` event handler."""

    @app.event("message")
    async def on_message(event: dict, client, context) -> None:  # noqa: ANN001
        # Ignore bot messages, subtype events (edits, joins, …), thread broadcasts.
        if event.get("bot_id") or event.get("subtype"):
            return

        channel_id = event.get("channel", "")
        user_id = event.get("user", "")
        raw_text = event.get("text", "")

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

        # Manual-input channels are human-first: a plain message stays as chat
        # and is NOT forwarded. The agent runs only when the message @-mentions
        # the bot (complementary to `/ccslack run`). Either way we strip the
        # bot mention so the agent receives a clean prompt.
        bot_user_id = context.get("bot_user_id") or ""
        mention = f"<@{bot_user_id}>" if bot_user_id else ""
        mentioned = bool(mention) and mention in raw_text
        if window_query.get_input_mode(window_id) == "manual" and not mentioned:
            return
        if mention:
            raw_text = raw_text.replace(mention, " ")

        # Slack encodes inbound text: it wraps auto-linked URLs and mentions in
        # <…> and escapes literal &,<,> as HTML entities. Decode before we type
        # it into tmux, or e.g. `git clone <https://…>` reaches bash with the
        # angle brackets intact and fails as a redirection.
        text = decode_slack_text(raw_text).strip()
        if not text:
            return

        ts = event.get("ts", "")
        # ✓ reaction confirms the keypress reached tmux. On the shell marker
        # path this is later replaced by ✅ / ❌ once the exit code lands.
        if await deliver_to_agent(client, channel_id, window_id, text, slack_ts=ts):
            await _react(client, channel_id, ts, "white_check_mark")
        else:
            await _react(client, channel_id, ts, "warning")


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
