"""`/ccslack panes` — list all panes in the bound window.

Walking-skeleton scope: just enumeration. ccgram additionally polls non-active
panes for blocked prompts and surfaces alerts; that's deferred — the pane list
is enough to confirm a multi-pane agent team is alive and inspect each pane
manually with the existing toolbar / send-keys flow.
"""

from __future__ import annotations

import contextlib
import structlog
from typing import Any

from slack_sdk.errors import SlackApiError

from ..thread_router import thread_router
from ..tmux_manager import tmux_manager

logger = structlog.get_logger()


async def handle_panes(
    client,  # noqa: ANN001 — Bolt-provided AsyncWebClient
    channel_id: str,
    user_id: str,
) -> None:
    """``/ccslack panes`` body."""
    window_id = thread_router.get_window_for_channel(channel_id)
    if window_id is None:
        await _ephemeral(client, channel_id, user_id, "ccslack: not a session channel.")
        return

    try:
        panes = await tmux_manager.list_panes(window_id)
    except OSError, RuntimeError:
        logger.exception("list_panes failed for %s", window_id)
        panes = []

    if not panes:
        await _ephemeral(
            client,
            channel_id,
            user_id,
            f"ccslack: window `{window_id}` has no listed panes (or it's gone).",
        )
        return

    blocks: list[dict[str, Any]] = [
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": (
                    f":pause_button: panes in `{window_id}` "
                    f"({len(panes)} pane{'s' if len(panes) != 1 else ''})"
                ),
            },
        },
        {"type": "divider"},
    ]
    for pane in panes:
        marker = ":sparkles:" if pane.active else ":small_blue_diamond:"
        cmd = pane.command or "(idle shell)"
        path = pane.path or "?"
        blocks.append(
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": (
                        f"{marker} *#{pane.index}* `{pane.pane_id}` · "
                        f"`{cmd}` · {pane.width}×{pane.height}\n"
                        f":file_folder: `{path}`"
                    ),
                },
            }
        )
    await _ephemeral(
        client,
        channel_id,
        user_id,
        f"panes in {window_id}: {len(panes)}",
        blocks=blocks,
    )


async def _ephemeral(
    client,  # noqa: ANN001
    channel_id: str,
    user_id: str,
    text: str,
    *,
    blocks: list[dict[str, Any]] | None = None,
) -> None:
    payload: dict[str, Any] = {"channel": channel_id, "user": user_id, "text": text}
    if blocks is not None:
        payload["blocks"] = blocks
    with contextlib.suppress(SlackApiError):
        await client.chat_postEphemeral(**payload)


__all__ = ["handle_panes"]
