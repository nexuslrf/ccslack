"""High-level send helpers — safe_post / safe_update / safe_send.

Wrap raw ``SlackClient`` calls with two policies:
  1. **Block Kit + fallback** — payload built via ``slack_formatting.to_blocks``;
     on SlackApiError the call retries with plain ``text=`` only.
  2. **Per-channel rate limiting** — at most one outbound message per ``channel``
     every ``MIN_INTERVAL_SECONDS`` (matches ccgram's 1.1s budget).

Splitting at the send layer: callers may pass arbitrarily long ``text``; this
module splits into multiple Block Kit posts when total characters exceed the
per-message ceiling.
"""

from __future__ import annotations

import asyncio
import structlog
import time
from typing import Any

from slack_sdk.errors import SlackApiError

from .slack_client import SlackClient
from .slack_formatting import (
    FALLBACK_TEXT_LIMIT,
    SECTION_TEXT_LIMIT,
    to_blocks,
)

logger = structlog.get_logger()

# Minimum seconds between two outbound messages per channel. Slack's tier-3
# limit is ~1 message/sec/channel; we keep a small headroom.
MIN_INTERVAL_SECONDS = 1.1

# Max characters per single post. We chunk above this. Slack's hard limit on
# the fallback text is ~40k, but we keep blocks-coherent posts well under it.
MAX_POST_CHARS = SECTION_TEXT_LIMIT * 4
# Slack rejects a message with more than 50 blocks. A MAX_POST_CHARS chunk
# yields far fewer, but cap defensively so pathological content degrades
# gracefully instead of erroring the whole post.
SLACK_MAX_BLOCKS = 50


def _cap_blocks(blocks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Bound a block list to Slack's 50-block limit, flagging any drop."""
    if len(blocks) <= SLACK_MAX_BLOCKS:
        return blocks
    dropped = len(blocks) - (SLACK_MAX_BLOCKS - 1)
    kept = blocks[: SLACK_MAX_BLOCKS - 1]
    kept.append(
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f"_… truncated {dropped} more block(s)_",
            },
        }
    )
    return kept

_last_sent: dict[str, float] = {}


async def rate_limit_send(channel: str) -> None:
    """Sleep until the per-channel rate budget allows the next send."""
    now = time.monotonic()
    last = _last_sent.get(channel, 0.0)
    delta = now - last
    if delta < MIN_INTERVAL_SECONDS:
        await asyncio.sleep(MIN_INTERVAL_SECONDS - delta)
    _last_sent[channel] = time.monotonic()


def split_message(text: str, max_chars: int = MAX_POST_CHARS) -> list[str]:
    """Split a long string into chunks <= ``max_chars`` on paragraph boundaries.

    Falls back to a hard split inside a paragraph when no boundary fits.
    """
    if len(text) <= max_chars:
        return [text]
    parts: list[str] = []
    remaining = text
    while remaining:
        if len(remaining) <= max_chars:
            parts.append(remaining)
            break
        # Prefer split at a double-newline within the budget.
        split = remaining.rfind("\n\n", 0, max_chars)
        if split < 0:
            split = remaining.rfind("\n", 0, max_chars)
        if split <= 0:
            split = max_chars
        parts.append(remaining[:split].rstrip())
        remaining = remaining[split:].lstrip("\n")
    return parts


async def safe_post(
    client: SlackClient,
    *,
    channel: str,
    text: str,
    blocks: list[dict[str, Any]] | None = None,
    thread_ts: str | None = None,
    **kwargs: Any,
) -> str | None:
    """Post a message with Block Kit + plain-text fallback.

    Returns the new message ``ts`` (timestamp), or ``None`` if the post failed
    entirely. When ``blocks`` is supplied it is sent verbatim; otherwise blocks
    are built from ``text``.

    Long ``text`` (over ``MAX_POST_CHARS``) is split on paragraph boundaries
    into a sequence of posts so nothing is truncated — newer models routinely
    emit answers past a single Slack message's capacity. The **first** part's
    ``ts`` is returned (the anchor callers pair/record against).
    """
    if blocks is None and len(text) > MAX_POST_CHARS:
        first_ts: str | None = None
        for part in split_message(text):
            ts = await _post_one(
                client, channel=channel, text=part, thread_ts=thread_ts, **kwargs
            )
            if first_ts is None:
                first_ts = ts
        return first_ts
    return await _post_one(
        client,
        channel=channel,
        text=text,
        blocks=blocks,
        thread_ts=thread_ts,
        **kwargs,
    )


async def _post_one(
    client: SlackClient,
    *,
    channel: str,
    text: str,
    blocks: list[dict[str, Any]] | None = None,
    thread_ts: str | None = None,
    **kwargs: Any,
) -> str | None:
    """Post exactly one Slack message (rate-limited) with block cap + fallback."""
    await rate_limit_send(channel)
    if blocks is None:
        built_blocks, fallback = to_blocks(text)
    else:
        built_blocks = blocks
        fallback = text[:FALLBACK_TEXT_LIMIT]

    payload: dict[str, Any] = {
        "channel": channel,
        "text": fallback or text[:FALLBACK_TEXT_LIMIT],
    }
    if built_blocks:
        payload["blocks"] = _cap_blocks(built_blocks)
    if thread_ts is not None:
        payload["thread_ts"] = thread_ts
    payload.update(kwargs)

    try:
        result = await client.chat_postMessage(**payload)
        return result.get("ts") if hasattr(result, "get") else result["ts"]
    except SlackApiError as exc:
        logger.warning(
            "chat_postMessage with blocks failed (%s); retrying as plain text",
            exc.response.get("error") if exc.response else exc,
        )
        try:
            payload.pop("blocks", None)
            result = await client.chat_postMessage(**payload)
            return result.get("ts") if hasattr(result, "get") else result["ts"]
        except SlackApiError:
            logger.exception("chat_postMessage plain-text fallback also failed")
            return None


async def safe_update(
    client: SlackClient,
    *,
    channel: str,
    ts: str,
    text: str,
    blocks: list[dict[str, Any]] | None = None,
    **kwargs: Any,
) -> bool:
    """Edit a message via chat.update with Block Kit + plain-text fallback."""
    if blocks is None:
        built_blocks, fallback = to_blocks(text)
    else:
        built_blocks = blocks
        fallback = text[:FALLBACK_TEXT_LIMIT]

    payload: dict[str, Any] = {
        "channel": channel,
        "ts": ts,
        "text": fallback or text[:FALLBACK_TEXT_LIMIT],
    }
    if built_blocks:
        # chat.update targets one message, so we can't spill into more posts —
        # cap to Slack's block limit (rare; only huge in-place tool-result edits).
        payload["blocks"] = _cap_blocks(built_blocks)
    payload.update(kwargs)

    try:
        await client.chat_update(**payload)
        return True
    except SlackApiError as exc:
        error = exc.response.get("error") if exc.response else str(exc)
        if error == "message_not_found":
            logger.debug("chat.update target missing: %s/%s", channel, ts)
            return False
        logger.warning(
            "chat.update with blocks failed (%s); retrying as plain text", error
        )
        try:
            payload.pop("blocks", None)
            await client.chat_update(**payload)
            return True
        except SlackApiError:
            logger.exception("chat.update plain-text fallback also failed")
            return False


async def safe_send_long(
    client: SlackClient,
    *,
    channel: str,
    text: str,
    thread_ts: str | None = None,
    **kwargs: Any,
) -> list[str]:
    """Post a long message as a sequence of split parts. Returns the ts list."""
    chunks = split_message(text)
    sent: list[str] = []
    for chunk in chunks:
        ts = await safe_post(
            client, channel=channel, text=chunk, thread_ts=thread_ts, **kwargs
        )
        if ts is not None:
            sent.append(ts)
    return sent


async def safe_close_message(
    client: SlackClient,
    *,
    channel: str,
    ts: str,
    label: str,
) -> None:
    """Close a bot-posted message: try ``chat.delete``, fall back to ``chat.update``.

    Some workspaces (Enterprise Grid retention / compliance locks) refuse
    ``chat.delete`` on bot messages even when ``chat:write`` is granted. The
    UX bug from that path is that "Close" / "Dismiss" buttons appear to do
    nothing. This helper keeps ``chat.delete`` as the preferred path (cleaner
    channel history) and falls through to a small ``:lock: _{label} closed_``
    stub via ``chat.update`` so the user always sees the click registered.
    """
    try:
        await client.chat_delete(channel=channel, ts=ts)
        return
    except SlackApiError as exc:
        error = exc.response.get("error") if exc.response else str(exc)
        logger.info(
            "chat.delete refused (%s); falling back to chat.update for %s",
            error,
            label,
        )

    closed_text = f":lock: _{label} closed_"
    blocks = [
        {"type": "context", "elements": [{"type": "mrkdwn", "text": closed_text}]}
    ]
    try:
        await client.chat_update(
            channel=channel, ts=ts, text=closed_text, blocks=blocks
        )
    except SlackApiError as exc:
        error = exc.response.get("error") if exc.response else str(exc)
        logger.warning("chat.update fallback also failed for %s: %s", label, error)


__all__ = [
    "MAX_POST_CHARS",
    "MIN_INTERVAL_SECONDS",
    "rate_limit_send",
    "safe_close_message",
    "safe_post",
    "safe_send_long",
    "safe_update",
    "split_message",
]
