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
    """
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
        payload["blocks"] = built_blocks
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
        payload["blocks"] = built_blocks
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


__all__ = [
    "MAX_POST_CHARS",
    "MIN_INTERVAL_SECONDS",
    "rate_limit_send",
    "safe_post",
    "safe_send_long",
    "safe_update",
    "split_message",
]
