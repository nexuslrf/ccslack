"""Render markdown tables from agent output as images.

Slack renders markdown tables poorly (pipes and dashes, no alignment). The raw
agent answer is always posted unchanged; when it contains a GitHub-flavored
markdown table, this module posts an extra prompt with a button. On click the
detected table(s) are laid out as an aligned monospace box and rendered to a
PNG via the screenshot text renderer, then uploaded to the channel.

Pipeline:
  * ``find_table_blocks(text)``  — locate markdown table blocks (fenced code
    skipped) without false-positiving on horizontal rules.
  * ``render_tables_png(blocks)`` — parse + align each table into a box, stack
    them, and rasterise to PNG bytes.
  * ``maybe_offer_table_render`` — post the "render as image?" prompt.
  * ``register(app)``            — wire the Render / Dismiss buttons.
"""

from __future__ import annotations

import contextlib
import io
import re
import structlog
import uuid
from typing import TYPE_CHECKING

from slack_sdk.errors import SlackApiError

from ..config import config
from ..slack_client import BoltSlackClient
from ..slack_sender import safe_post

if TYPE_CHECKING:
    from slack_bolt.async_app import AsyncApp

    from ..slack_client import SlackClient

logger = structlog.get_logger()

# A markdown table delimiter row: cells of dashes with optional alignment colons,
# at least two columns. e.g. ``| :--- | ---: |`` or ``--- | ---``.
_DELIM_RE = re.compile(r"^\s*\|?\s*:?-{1,}:?\s*(?:\|\s*:?-{1,}:?\s*)+\|?\s*$")
_FENCE_RE = re.compile(r"^\s*(?:```|~~~)")

# A real table needs at least this many columns — guards against treating a
# lone ``---`` horizontal rule as a single-column table.
_MIN_TABLE_COLUMNS = 2

# Pending render jobs keyed by a short token carried in the button value.
# token -> (channel_id, [raw_table_blocks]). Bounded to avoid unbounded growth.
_PENDING: dict[str, tuple[str, list[str]]] = {}
_PENDING_MAX = 256


def _split_row(line: str) -> list[str]:
    """Split a markdown table row into trimmed cells (outer pipes dropped)."""
    s = line.strip()
    if s.startswith("|"):
        s = s[1:]
    if s.endswith("|"):
        s = s[:-1]
    return [cell.strip() for cell in s.split("|")]


def find_table_blocks(text: str) -> list[str]:
    """Return raw markdown table blocks found in *text* (fenced code skipped).

    A block is a header line, a delimiter line, and the contiguous data rows
    below it. The header and delimiter must agree on column count (≥2) so a
    lone ``---`` horizontal rule isn't mistaken for a table.
    """
    lines = text.split("\n")
    blocks: list[str] = []
    in_fence = False
    i = 0
    n = len(lines)
    while i < n:
        if _FENCE_RE.match(lines[i]):
            in_fence = not in_fence
            i += 1
            continue
        if (
            not in_fence
            and i > 0
            and _DELIM_RE.match(lines[i])
            and "|" in lines[i - 1]
            and len(_split_row(lines[i])) >= _MIN_TABLE_COLUMNS
            and len(_split_row(lines[i - 1])) == len(_split_row(lines[i]))
        ):
            header = lines[i - 1]
            data: list[str] = []
            j = i + 1
            while j < n and "|" in lines[j] and lines[j].strip() and not _FENCE_RE.match(lines[j]):
                data.append(lines[j])
                j += 1
            if data:  # a header+delimiter with no rows isn't worth rendering
                blocks.append("\n".join([header, lines[i], *data]))
            i = j
            continue
        i += 1
    return blocks


def _parse_aligns(delim_line: str) -> list[str]:
    aligns: list[str] = []
    for cell in _split_row(delim_line):
        c = cell.strip()
        left, right = c.startswith(":"), c.endswith(":")
        aligns.append("center" if left and right else "right" if right else "left")
    return aligns


def _parse_table(block: str) -> tuple[list[list[str]], list[str]]:
    """Parse a raw table block into (rows-including-header, per-column aligns)."""
    rows_raw = [ln for ln in block.split("\n") if ln.strip()]
    header = _split_row(rows_raw[0])
    aligns = _parse_aligns(rows_raw[1]) if len(rows_raw) > 1 else []
    body = [_split_row(ln) for ln in rows_raw[2:]]
    rows = [header, *body]
    cols = max(len(r) for r in rows)
    rows = [r + [""] * (cols - len(r)) for r in rows]
    aligns = (aligns + ["left"] * cols)[:cols]
    return rows, aligns


def _table_to_monospace(rows: list[list[str]], aligns: list[str]) -> str:
    """Lay out parsed rows as an aligned box-drawing table."""
    cols = len(rows[0])
    widths = [max(len(rows[r][c]) for r in range(len(rows))) for c in range(cols)]

    def cell(text: str, width: int, align: str) -> str:
        pad = width - len(text)
        if align == "right":
            return " " * pad + text
        if align == "center":
            left = pad // 2
            return " " * left + text + " " * (pad - left)
        return text + " " * pad

    def border(left: str, mid: str, right: str) -> str:
        return left + mid.join("─" * (widths[c] + 2) for c in range(cols)) + right

    def row_line(cells: list[str]) -> str:
        body = " │ ".join(cell(cells[c], widths[c], aligns[c]) for c in range(cols))
        return f"│ {body} │"

    out = [border("┌", "┬", "┐"), row_line(rows[0]), border("├", "┼", "┤")]
    out.extend(row_line(r) for r in rows[1:])
    out.append(border("└", "┴", "┘"))
    return "\n".join(out)


async def render_tables_png(blocks: list[str]) -> bytes | None:
    """Render the given table blocks to a single PNG (stacked). None on failure."""
    monospace = "\n\n".join(
        _table_to_monospace(*_parse_table(block)) for block in blocks
    )
    if not monospace.strip():
        return None
    # Lazy: the renderer pulls Pillow + bundled fonts.
    from ..screenshot import text_to_image

    try:
        return await text_to_image(monospace, with_ansi=False)
    except (OSError, ValueError):
        logger.exception("table_render: text_to_image failed")
        return None


def _remember(token: str, channel_id: str, blocks: list[str]) -> None:
    if len(_PENDING) >= _PENDING_MAX:
        # Drop the oldest entry (insertion-ordered dict).
        with contextlib.suppress(StopIteration):
            del _PENDING[next(iter(_PENDING))]
    _PENDING[token] = (channel_id, blocks)


async def maybe_offer_table_render(
    client: SlackClient, channel_id: str, text: str
) -> None:
    """Post a "render table as image?" prompt when *text* contains a table."""
    if not config.table_render_offer:
        return
    blocks = find_table_blocks(text)
    if not blocks:
        return
    token = uuid.uuid4().hex[:12]
    _remember(token, channel_id, blocks)
    count = len(blocks)
    label = "table" if count == 1 else f"{count} tables"
    await safe_post(
        client,
        channel=channel_id,
        text=f":bar_chart: Detected a markdown {label} — render as an image?",
        blocks=[
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": (
                        f":bar_chart: The message above contains a markdown "
                        f"{label}. Render it as an image for easier reading?"
                    ),
                },
            },
            {
                "type": "actions",
                "block_id": f"ccslack_table_actions:{token}",
                "elements": [
                    {
                        "type": "button",
                        "action_id": "ccslack_render_table",
                        "style": "primary",
                        "text": {"type": "plain_text", "text": ":frame_with_picture: Render image"},
                        "value": token,
                    },
                    {
                        "type": "button",
                        "action_id": "ccslack_render_table_dismiss",
                        "text": {"type": "plain_text", "text": ":x: Dismiss"},
                        "value": token,
                    },
                ],
            },
        ],
    )


def _button_token(body: dict, action_id: str) -> str:
    for action in body.get("actions", []) or []:
        if action.get("action_id") == action_id:
            return action.get("value", "")
    return ""


def register(app: AsyncApp) -> None:
    """Wire the table render / dismiss button actions."""

    @app.action("ccslack_render_table")
    async def on_render(ack, body, client) -> None:  # noqa: ANN001
        await ack()
        user_id = body.get("user", {}).get("id", "")
        channel_id = body.get("channel", {}).get("id", "")
        message_ts = (body.get("message") or {}).get("ts", "")
        from .auth import is_authorized

        if not is_authorized(user_id, channel_id) or not channel_id:
            return
        token = _button_token(body, "ccslack_render_table")
        entry = _PENDING.pop(token, None)
        if entry is None:
            with contextlib.suppress(SlackApiError):
                await client.chat_postEphemeral(
                    channel=channel_id,
                    user=user_id,
                    text="ccslack: this table render offer has expired.",
                )
            return

        _, blocks = entry
        png = await render_tables_png(blocks)
        if png is None:
            with contextlib.suppress(SlackApiError):
                await client.chat_postEphemeral(
                    channel=channel_id,
                    user=user_id,
                    text="ccslack: couldn't render the table (check logs).",
                )
            return

        bolt_client = BoltSlackClient(client)
        try:
            await bolt_client.files_upload_v2(
                channel=channel_id,
                file=io.BytesIO(png),
                filename="table.png",
                title="Rendered table",
            )
        except SlackApiError as exc:
            logger.warning(
                "table_render upload failed: %s",
                exc.response.get("error") if exc.response else exc,
            )
            return

        # Collapse the prompt once rendered so it can't be re-clicked.
        if message_ts:
            with contextlib.suppress(SlackApiError):
                await client.chat_update(
                    channel=channel_id,
                    ts=message_ts,
                    text=":frame_with_picture: Table rendered as image above.",
                    blocks=[],
                )

    @app.action("ccslack_render_table_dismiss")
    async def on_dismiss(ack, body, client) -> None:  # noqa: ANN001
        await ack()
        channel_id = body.get("channel", {}).get("id", "")
        message_ts = (body.get("message") or {}).get("ts", "")
        token = _button_token(body, "ccslack_render_table_dismiss")
        _PENDING.pop(token, None)
        if message_ts and channel_id:
            with contextlib.suppress(SlackApiError):
                await client.chat_delete(channel=channel_id, ts=message_ts)


__all__ = [
    "find_table_blocks",
    "maybe_offer_table_render",
    "register",
    "render_tables_png",
]
