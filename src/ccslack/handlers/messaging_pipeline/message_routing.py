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
    if notification_mode == "silent":
        # Silent suppresses everything from tmux — even tool flows. The channel
        # only gets status-pill updates; drive/monitor via /toolbar + /screenshot.
        return True
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

    for channel_id, window_id in channels:
        structlog.contextvars.clear_contextvars()
        structlog.contextvars.bind_contextvars(
            window_id=window_id, channel_id=channel_id, session_id=msg.session_id
        )
        await _route_to_channel(client, channel_id, window_id, msg, text)


async def _pre_post_suppressed(
    client: SlackClient,
    channel_id: str,
    window_id: str,
    msg: NewMessage,
    text: str,
) -> bool:
    """Return True when *msg* must not get a normal channel post.

    Consolidates every pre-post gate — each also refreshes the status pill:

      * **silent** mode — suppress *everything* from tmux, including the live
        picker; the channel only shows the status pill (monitor via
        ``/toolbar`` + ``/screenshot``).
      * interactive **live picker** — a ``tool_use`` for an interactive tool
        drives the picker instead of a plain post.
      * picker **close** — a ``tool_result`` that resolves an open picker.
      * per-window **tool-call visibility** (``/ccslack toolcalls``).
      * **notification mode** (``/ccslack mute`` — all/errors_only/muted).
    """
    # Lazy: interactive pulls picker machinery; keep it off the hot import path.
    from ..interactive import (
        INTERACTIVE_TOOL_NAMES,
        enter_interactive_mode,
        maybe_exit_for_tool_result,
    )
    from ..status import update_status

    notification_mode = window_query.get_notification_mode(window_id)

    if notification_mode == "silent":
        await update_status(client, channel_id, window_id, "active")
        return True

    if msg.content_type == "tool_use" and (msg.tool_name or "") in INTERACTIVE_TOOL_NAMES:
        await enter_interactive_mode(
            client,
            channel_id=channel_id,
            window_id=window_id,
            tool_use_id=msg.tool_use_id,
            tool_name=msg.tool_name or "",
        )
        await update_status(client, channel_id, window_id, "active")
        return True

    if (
        msg.content_type == "tool_result"
        and msg.tool_use_id
        and await maybe_exit_for_tool_result(client, msg.tool_use_id)
    ):
        await update_status(client, channel_id, window_id, "active")
        return True

    if msg.content_type in (
        "tool_use",
        "tool_result",
    ) and window_query.is_tool_calls_hidden(window_id):
        logger.debug("Tool calls hidden for window %s; skipping", window_id)
        await update_status(client, channel_id, window_id, "active")
        return True

    if _should_skip_for_mode(notification_mode, text, msg):
        logger.debug(
            "Suppressing message for window %s (mode=%s)",
            window_id,
            notification_mode,
        )
        await update_status(client, channel_id, window_id, "active")
        return True

    return False


async def _route_to_channel(
    client: SlackClient,
    channel_id: str,
    window_id: str,
    msg: NewMessage,
    text: str,
) -> None:
    """Deliver one message to one bound channel, applying all routing policy."""
    # Lazy: status / polling modules pull session_manager + slack helpers.
    from ..polling.coordinator import mark_active
    from ..status import update_status
    from . import turn_threads

    mark_active(window_id)

    # A fresh user message closes the previous turn's tool thread (if any)
    # so the new exchange starts with a clean parent.
    if msg.role == "user":
        await turn_threads.note_user_message(client, channel_id)
        # New conversation round — scopes the per-response purge button.
        from .. import purge

        purge.bump_round(channel_id)

        # A prompt sent via `/ccslack run` is quiet: drop its echo so nothing
        # visible marks it (the run invocation was ephemeral). @-mentions and
        # typed messages keep their echo.
        from .. import run_echo

        if run_echo.consume_user_echo_suppression(window_id):
            return

    # Everything that decides "don't post this normally" (silent mode, the live
    # picker, tool-call visibility, notification modes) lives in one gate so the
    # post path below stays linear.
    if await _pre_post_suppressed(client, channel_id, window_id, msg, text):
        return

    # Tool-call threading: route tool_use / tool_result / thinking under a
    # per-turn thread parent in the main channel. Plain text answers stay in
    # the main channel (thread_ts None); the parent is created lazily on the
    # first threadable message of the turn.
    thread_ts: str | None = None
    if (
        msg.role != "user"
        and msg.content_type in ("tool_use", "tool_result", "thinking")
        and window_query.is_tool_threading_enabled(window_id)
    ):
        thread_ts = await turn_threads.thread_parent_for(
            client, channel_id, is_tool=msg.content_type == "tool_use"
        )

    # Public-channel mode: post the round's single "purge" button just BEFORE
    # its first answer so it sits above the responses (deduped per round).
    from ...config import config

    if config.public_channels and msg.role != "user" and msg.content_type == "text":
        from .. import purge

        await purge.post_response_button(client, channel_id)

    decorated = _decorate(msg, text)
    await _post_or_pair(client, channel_id, msg, decorated, thread_ts=thread_ts)

    # Offer to render any markdown table in a plain agent answer as an image
    # (Slack renders tables poorly). The raw text is already posted above; this
    # only adds an opt-in button. User echoes / tool flows are skipped.
    if msg.role != "user" and msg.content_type == "text":
        from ..table_render import maybe_offer_table_render

        await maybe_offer_table_render(client, channel_id, text)

    # No-hooks turn-end signal: the agent's final answer closes the thread.
    # The Stop hook also calls end_turn (idempotent), so this is just a
    # backstop for providers / setups without hooks.
    if msg.content_type == "text" and msg.phase == "final_answer":
        await turn_threads.end_turn(client, channel_id)

    # Flip status to "active" whenever the agent is producing output. The Stop
    # hook flips it to "done"; idle is the resting state between turns.
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


def _purge_kind(msg: NewMessage) -> str:
    """Map a transcript message to a purge-ledger kind."""
    if msg.role == "user":
        return "echo"
    if msg.content_type in ("tool_use", "tool_result"):
        return "tool"
    if msg.content_type == "thinking":
        return "thinking"
    return "answer"


async def _post_or_pair(
    client: SlackClient,
    channel_id: str,
    msg: NewMessage,
    decorated: str,
    *,
    thread_ts: str | None = None,
) -> str | None:
    """Post a transcript chunk OR pair it with a prior tool_use.

    ``thread_ts`` (when set) lands the message inside a Slack thread — used by
    the tool-call threading feature to group a turn's chain under one parent.

    Pairing rules:

      * ``tool_use``    — post, record (channel, ts, text) under
        ``_TOOL_USE_MEMO[session_id][tool_use_id]`` so the eventual result can
        rewrite the same message in place. ``chat.update`` targets the message
        ts directly, so it works whether or not the message is threaded.
      * ``tool_result`` — if we have a matching memo, ``chat.update`` the
        original tool_use message to include the result inline. Otherwise post
        as a standalone message (the tool_use may have streamed during a bot
        restart).
      * Everything else — straight post.
    """
    from .. import purge

    if msg.content_type == "tool_use" and msg.tool_use_id:
        ts = await safe_post(
            client, channel=channel_id, text=decorated, thread_ts=thread_ts
        )
        if ts is not None:
            _TOOL_USE_MEMO.setdefault(msg.session_id, {})[msg.tool_use_id] = (
                channel_id,
                ts,
                decorated,
            )
            purge.record(channel_id, ts, thread_ts=thread_ts, kind="tool")
        return ts

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
                    # No new message — the tool_use ts is already recorded.
                    return prior_ts
        # No memo or chat.update failed — fall through to fresh post.

    ts = await safe_post(client, channel=channel_id, text=decorated, thread_ts=thread_ts)
    kind = _purge_kind(msg)
    # Keep the echo's text so a purge can annotate it in place (not delete it).
    purge.record(
        channel_id,
        ts,
        thread_ts=thread_ts,
        kind=kind,
        text=decorated if kind == "echo" else None,
    )
    return ts


__all__ = ["handle_new_message"]
