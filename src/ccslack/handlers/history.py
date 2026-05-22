"""`/ccslack history` — print recent transcript messages into the session channel.

ccgram has a fully paginated browser with per-message expandable quotes;
ccslack ships a simpler version: load the last N messages (default 20, max 100),
render each as one Block Kit context block, post the whole thing as one
message. Pagination via Older/Newer buttons can land later.
"""

from __future__ import annotations

import contextlib
import structlog
from typing import Any

from slack_sdk.errors import SlackApiError

from .. import session_query
from ..thread_router import thread_router

logger = structlog.get_logger()

_DEFAULT_LIMIT = 20
_MAX_LIMIT = 100
_TEXT_TRUNCATE = 600


def _truncate(text: str, limit: int = _TEXT_TRUNCATE) -> str:
    text = text.strip()
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "…"


def _format_message(msg: dict[str, Any]) -> str:
    role = msg.get("role") or "?"
    content_type = msg.get("content_type") or "text"
    text = _truncate(str(msg.get("text") or ""))
    if content_type == "thinking":
        prefix = ":thought_balloon:"
    elif content_type == "tool_use":
        prefix = ":wrench:"
    elif content_type == "tool_result":
        prefix = ":receipt:"
    elif role == "user":
        prefix = ":bust_in_silhouette:"
    else:
        prefix = ":robot_face:"
    return f"{prefix} *{role}* · `{content_type}`\n{text}"


async def handle_history(
    client,  # noqa: ANN001 — Bolt-provided AsyncWebClient
    channel_id: str,
    user_id: str,
    raw_limit: str,
) -> None:
    """``/ccslack history [N]`` body."""
    try:
        limit = int(raw_limit) if raw_limit else _DEFAULT_LIMIT
    except ValueError:
        limit = _DEFAULT_LIMIT
    limit = max(1, min(limit, _MAX_LIMIT))

    window_id = thread_router.get_window_for_channel(channel_id)
    if window_id is None:
        await _ephemeral(
            client,
            channel_id,
            user_id,
            "ccslack: `history` only works in a session channel.",
        )
        return

    messages, total = await session_query.get_recent_messages(window_id)
    if not messages:
        await _ephemeral(
            client,
            channel_id,
            user_id,
            "ccslack: no transcript yet for this session.",
        )
        return

    recent = messages[-limit:]
    blocks: list[dict[str, Any]] = [
        {
            "type": "context",
            "elements": [
                {
                    "type": "mrkdwn",
                    "text": (
                        f":books: last *{len(recent)}* of *{total}* messages · "
                        f"`{window_id}`"
                    ),
                }
            ],
        },
        {"type": "divider"},
    ]
    for msg in recent:
        blocks.append(
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": _format_message(msg)},
            }
        )
        blocks.append({"type": "divider"})

    # Slack caps blocks at 50 per message — chunk if needed.
    fallback = f"history — last {len(recent)} of {total}"
    for chunk in _chunked(blocks, 48):
        with contextlib.suppress(SlackApiError):
            await client.chat_postEphemeral(
                channel=channel_id, user=user_id, text=fallback, blocks=chunk
            )


def _chunked(items: list, n: int) -> list[list]:
    return [items[i : i + n] for i in range(0, len(items), n)]


async def _ephemeral(client, channel_id: str, user_id: str, text: str) -> None:  # noqa: ANN001
    with contextlib.suppress(SlackApiError):
        await client.chat_postEphemeral(channel=channel_id, user=user_id, text=text)


__all__ = ["handle_history"]
