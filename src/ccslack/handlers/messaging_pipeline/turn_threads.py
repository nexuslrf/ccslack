"""Per-channel agent-turn threading for tool chains.

When tool-call threading is enabled (``window_query.is_tool_threading_enabled``),
an agent turn's noisy chain — ``tool_use`` / ``tool_result`` / ``thinking`` —
is collapsed under a single parent message in the main channel and posted into
its Slack thread. Plain text answers and interactive prompts stay in the main
channel so the conversation reads cleanly.

A "turn" is the activity between two boundaries:
  * starts lazily on the first threadable message after a (re)start,
  * ends on the agent's Stop hook (``end_turn``) or the next user message
    (``note_user_message`` → ``end_turn``).

On turn end the parent is rewritten into a one-line summary (``N tool calls``).
A turn with zero tool calls (only thinking) is summarised as plain activity.

State is per-channel and in-memory only; a bot restart simply starts fresh
turns (any orphaned parent just keeps its "running…" text, harmless).
"""

from __future__ import annotations

import structlog
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ...slack_client import SlackClient

logger = structlog.get_logger()

_RUNNING_TEXT = ":hammer_and_wrench: *Tool activity* — running… _(expand thread)_"


def _parent_blocks(text: str) -> list[dict]:
    """Parent-message blocks with a Close button that deletes the whole thread.

    The button carries no id — the action handler uses the parent message's own
    ts (which *is* the thread's parent ts) to purge the thread.
    """
    return [
        {"type": "section", "text": {"type": "mrkdwn", "text": text}},
        {
            "type": "actions",
            "block_id": "ccslack_thread_actions",
            "elements": [
                {
                    "type": "button",
                    "action_id": "ccslack_purge_thread",
                    "style": "danger",
                    "text": {"type": "plain_text", "text": ":wastebasket: Close thread"},
                    "value": "thread",
                }
            ],
        },
    ]


@dataclass
class _Turn:
    """In-memory state for one agent turn in one channel."""

    parent_ts: str
    tool_count: int = 0
    started_at: float = field(default_factory=time.monotonic)


# channel_id -> active turn (None/absent = no turn in progress).
_turns: dict[str, _Turn] = {}


def has_active_turn(channel_id: str) -> bool:
    """True iff a turn (thread parent) is currently open for the channel."""
    return channel_id in _turns


def clear_channel(channel_id: str) -> None:
    """Drop turn state for a channel without touching Slack (archive / unbind)."""
    _turns.pop(channel_id, None)


def reset_for_testing() -> None:
    """Clear all turn state. Used by unit tests."""
    _turns.clear()


async def thread_parent_for(
    client: SlackClient,
    channel_id: str,
    *,
    is_tool: bool,
) -> str | None:
    """Return the thread_ts to post a threadable message under.

    Creates the parent message on the first threadable message of a turn.
    Returns ``None`` only when the parent couldn't be posted (Slack error);
    callers fall back to posting in the main channel.

    ``is_tool`` increments the turn's tool counter (so ``thinking`` posts
    thread without inflating the "N tool calls" summary).
    """
    turn = _turns.get(channel_id)
    if turn is None:
        # Lazy: slack_sender pulls config; import at call site.
        from ...slack_sender import safe_post

        ts = await safe_post(
            client,
            channel=channel_id,
            text=_RUNNING_TEXT,
            blocks=_parent_blocks(_RUNNING_TEXT),
        )
        if not ts:
            return None
        turn = _Turn(parent_ts=ts)
        _turns[channel_id] = turn
        # Record the parent so /ccslack purge + the thread Close button can
        # delete the whole thread (its tool replies carry thread_ts=parent).
        from .. import purge

        purge.record(channel_id, ts, thread_ts=ts, kind="thread_parent")
    if is_tool:
        turn.tool_count += 1
    return turn.parent_ts


async def end_turn(client: SlackClient, channel_id: str) -> None:
    """Finalise the current turn's parent into a summary and clear state.

    No-op when no turn is open. Best-effort: a failed summary edit is logged
    and the state is still cleared so the next turn starts clean.
    """
    turn = _turns.pop(channel_id, None)
    if turn is None:
        return
    # Lazy: slack_sender pulls config; import at call site.
    from ...slack_sender import safe_update

    if turn.tool_count:
        plural = "s" if turn.tool_count != 1 else ""
        summary = f":hammer_and_wrench: *{turn.tool_count} tool call{plural}* · done"
    else:
        summary = ":speech_balloon: *Agent activity* · done"
    # Keep the Close button on the finished thread.
    await safe_update(
        client,
        channel=channel_id,
        ts=turn.parent_ts,
        text=summary,
        blocks=_parent_blocks(summary),
    )


async def note_user_message(client: SlackClient, channel_id: str) -> None:
    """End any open turn when a fresh user message starts a new exchange."""
    await end_turn(client, channel_id)


__all__ = [
    "clear_channel",
    "end_turn",
    "has_active_turn",
    "note_user_message",
    "reset_for_testing",
    "thread_parent_for",
]
