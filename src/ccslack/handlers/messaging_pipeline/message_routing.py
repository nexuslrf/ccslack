"""Route SessionMonitor NewMessage events to Slack channels."""

from __future__ import annotations

import re
import structlog
from typing import TYPE_CHECKING

import structlog.contextvars

from ... import session_query, window_query
from ...session_monitor import NewMessage
from ...slack_sender import safe_post, safe_update

if TYPE_CHECKING:
    from ...slack_client import SlackClient

logger = structlog.get_logger()

# Skip tiny "thinking" snippets so the channel doesn't fill with hesitation noise.
_MIN_THINKING_LENGTH = 20

# Visual prefix for echoes of the user's own prompt (transcript role="user").
# Mirrors ccgram's ``\U0001f464`` (:bust_in_silhouette:) prefix so the channel
# clearly separates "this is what you said" from "this is what the agent said".
_USER_ECHO_PREFIX = ":bust_in_silhouette: "
# ccgram caps user-message echoes at 3000 chars to keep the channel tidy.
_MAX_USER_ECHO_LEN = 3000

# Per-session tool_use_id → (channel_id, slack_ts, decorated_tool_use_text).
# Populated when we post a ``tool_use`` message; consumed when the matching
# ``tool_result`` arrives. We rewrite the original message in place by
# ``chat.update`` so the channel reads as one tool call + result chunk.
#
# Entries are dropped on consumption; orphaned ones leak until the session ends.
# For walking-skeleton scope that's acceptable — agents typically pair every
# tool_use with a result within seconds, and the dict is bounded by tool calls
# per session.
_TOOL_USE_MEMO: dict[str, dict[str, tuple[str, str, str]]] = {}

# Keywords that bypass ``errors_only`` muting — port from ccgram.
_ERROR_KEYWORDS_RE = re.compile(
    r"\b(?:error|exception|failed|traceback|stderr|assertion)\b",
    re.IGNORECASE,
)


def _should_skip_for_mode(
    notification_mode: str,
    text: str,
    msg: NewMessage,
) -> bool:
    """True when ``notification_mode`` says this message should be suppressed."""
    if notification_mode == "muted":
        # Don't drop tool flows in muted mode — agents stop progressing without
        # explicit acknowledgement.
        is_tool_flow = msg.content_type in ("tool_use", "tool_result")
        return not is_tool_flow
    if notification_mode == "errors_only":
        if msg.content_type in ("tool_use", "tool_result"):
            # Tool flows always pass through.
            return False
        if msg.phase == "final_answer":
            return False
        return not _ERROR_KEYWORDS_RE.search(text)
    return False


async def handle_new_message(msg: NewMessage, client: SlackClient) -> None:
    """Handle a new assistant message — post to every bound Slack channel.

    Policy:
      * tool_use / tool_result visibility is per-window
        (``WindowState.tool_call_visibility`` ∈ ``default|shown|hidden``);
        ``default`` falls back to ``config.hide_tool_calls``. Tool messages get
        paired (the eventual result rewrites the original tool_use message in
        place — see ``_post_or_pair``).
      * short "thinking" snippets (< 20 chars) are dropped globally.
      * interactive ``tool_use`` (AskUserQuestion / ExitPlanMode /
        request_user_input) skip the normal post and drive the live picker
        instead — see ``handlers.interactive``.
      * everything else is posted with Block Kit + plain-text fallback.
    """

    text = msg.text or ""
    if not text.strip():
        return

    if msg.content_type == "thinking" and len(text.strip()) < _MIN_THINKING_LENGTH:
        return

    channels = session_query.find_channels_for_session(msg.session_id)
    if not channels:
        logger.debug("No Slack channels bound to session %s", msg.session_id)
        return

    # Lazy: status / polling modules pull session_manager + slack helpers.
    from ..polling.coordinator import mark_active
    from ..status import update_status

    # Lazy: interactive subsystem pulls slack_sender + tmux. Imported here so
    # tests that bypass message_routing don't pay the cost.
    from ..interactive import (
        INTERACTIVE_TOOL_NAMES,
        enter_interactive_mode,
        maybe_exit_for_tool_result,
    )

    for channel_id, window_id in channels:
        mark_active(window_id)
        structlog.contextvars.clear_contextvars()
        structlog.contextvars.bind_contextvars(
            window_id=window_id, channel_id=channel_id, session_id=msg.session_id
        )

        # tool_use of an interactive tool name → enter the live picker. Skip
        # the normal post; the picker carries the pane content live.
        if (
            msg.content_type == "tool_use"
            and (msg.tool_name or "") in INTERACTIVE_TOOL_NAMES
        ):
            await enter_interactive_mode(
                client,
                channel_id=channel_id,
                window_id=window_id,
                tool_use_id=msg.tool_use_id,
                tool_name=msg.tool_name or "",
            )
            await update_status(client, channel_id, window_id, "active")
            continue

        # tool_result paired with an active interactive picker → close picker
        # and skip the normal post (the picker now shows the final state).
        if (
            msg.content_type == "tool_result"
            and msg.tool_use_id
            and await maybe_exit_for_tool_result(client, msg.tool_use_id)
        ):
            await update_status(client, channel_id, window_id, "active")
            continue

        # Per-window tool-call visibility — moved inside the loop so each
        # channel can independently mute/show its tool chain via
        # ``/ccslack toolcalls``.
        if msg.content_type in (
            "tool_use",
            "tool_result",
        ) and window_query.is_tool_calls_hidden(window_id):
            logger.debug(
                "Tool calls hidden for window %s; skipping", window_id
            )
            await update_status(client, channel_id, window_id, "active")
            continue

        notification_mode = window_query.get_notification_mode(window_id)
        if _should_skip_for_mode(notification_mode, text, msg):
            logger.debug(
                "Suppressing message for window %s (mode=%s)",
                window_id,
                notification_mode,
            )
            await update_status(client, channel_id, window_id, "active")
            continue

        decorated = _decorate(msg, text)
        await _post_or_pair(client, channel_id, msg, decorated)

        # Flip status to "active" whenever the agent is producing output. The
        # Stop hook flips it to "done"; idle is the resting state between turns.
        await update_status(client, channel_id, window_id, "active")


def _decorate(msg: NewMessage, text: str) -> str:
    """Prefix transcript content with a small marker indicating its type.

    Role takes precedence over content_type — a user-role echo always gets the
    bust-in-silhouette prefix so the reader never confuses it with the agent's
    response. ccgram applies the same rule (see ``build_response_parts``).
    """
    if msg.role == "user":
        body = text.strip()
        if len(body) > _MAX_USER_ECHO_LEN:
            body = body[:_MAX_USER_ECHO_LEN] + "…"
        return f"{_USER_ECHO_PREFIX}{body}"
    if msg.content_type == "thinking":
        return f":thought_balloon: _{text.strip()}_"
    if msg.content_type == "tool_use":
        tool = msg.tool_name or "tool"
        return f":wrench: *{tool}*\n{text}"
    if msg.content_type == "tool_result":
        return f":receipt: ```{text}```"
    return text


async def _post_or_pair(
    client: SlackClient,
    channel_id: str,
    msg: NewMessage,
    decorated: str,
) -> None:
    """Post a transcript chunk OR pair it with a prior tool_use.

    Pairing rules:

      * ``tool_use``    — post, record (channel, ts, text) under
        ``_TOOL_USE_MEMO[session_id][tool_use_id]`` so the eventual result can
        rewrite the same message in place.
      * ``tool_result`` — if we have a matching memo, ``chat.update`` the
        original tool_use message to include the result inline. Otherwise post
        as a standalone message (the tool_use may have streamed during a bot
        restart).
      * Everything else — straight post.
    """
    if msg.content_type == "tool_use" and msg.tool_use_id:
        ts = await safe_post(client, channel=channel_id, text=decorated)
        if ts is not None:
            _TOOL_USE_MEMO.setdefault(msg.session_id, {})[msg.tool_use_id] = (
                channel_id,
                ts,
                decorated,
            )
        return

    if msg.content_type == "tool_result" and msg.tool_use_id:
        session_memo = _TOOL_USE_MEMO.get(msg.session_id, {})
        prior = session_memo.pop(msg.tool_use_id, None)
        if prior is not None:
            prior_channel, prior_ts, prior_text = prior
            if prior_channel == channel_id:
                combined = f"{prior_text}\n\n{decorated}"
                ok = await safe_update(
                    client, channel=channel_id, ts=prior_ts, text=combined
                )
                if ok:
                    return
        # No memo or chat.update failed — fall through to fresh post.

    await safe_post(client, channel=channel_id, text=decorated)


__all__ = ["handle_new_message"]
