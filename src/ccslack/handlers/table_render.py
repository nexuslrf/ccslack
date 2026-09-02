"""Render markdown tables from agent output as native Slack table blocks.

Slack renders markdown tables poorly (pipes and dashes, no alignment). The raw
agent answer is always posted unchanged; when it contains a GitHub-flavored
markdown table, this module posts an extra prompt with a button. On click the
detected table(s) are posted as native Slack "table" blocks (beta Block Kit —
https://docs.slack.dev/reference/block-kit/blocks/table-block). When a table
exceeds the native limits (100 rows, 20 cols, 10k chars, 1 table/message) or
the API rejects the block, it falls back to the legacy PNG renderer.

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
from ..slack_formatting import to_blocks
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
            while (
                j < n
                and "|" in lines[j]
                and lines[j].strip()
                and not _FENCE_RE.match(lines[j])
            ):
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
    from . import purge

    ts = await safe_post(
        client,
        channel=channel_id,
        text=f":bar_chart: Detected a markdown {label} — display as table?",
        blocks=[
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": (
                        f":bar_chart: The message above contains a markdown "
                        f"{label}. Display it as a native table?"
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
                        "text": {
                            "type": "plain_text",
                            "text": ":table_tennis: Display as table",
                        },
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
    purge.record(channel_id, ts, kind="control")


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
        # Prefer native Slack table blocks — no image generation, selectable
        # text, respects markdown column alignment. Falls back to PNG when a
        # table exceeds native limits or the API rejects the block.
        if await _post_native_tables(client, channel_id, blocks):
            if message_ts:
                with contextlib.suppress(SlackApiError):
                    await client.chat_update(
                        channel=channel_id,
                        ts=message_ts,
                        text=":table_tennis: Table displayed above.",
                        blocks=[],
                    )
            return

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
            result = await bolt_client.files_upload_v2(
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

        # Attach a one-click Remove button so the rendered image can be deleted.
        from . import purge

        file_id = purge.file_id_from_upload(result)
        if file_id:
            await purge.post_file_close_button(bolt_client, channel_id, file_id)

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


# ── Native Slack table blocks ───────────────────────────────────────────
# Slack's Block Kit "table" block (beta) renders tabular data natively — no
# image generation. Cell types: raw_text / raw_number / rich_text. Limits:
# 100 rows, 20 columns, 10,000 chars per table, ONE table per message.
# Docs: https://docs.slack.dev/reference/block-kit/blocks/table-block/

_TABLE_MAX_ROWS = 100
_TABLE_MAX_COLS = 20
_TABLE_MAX_CHARS = 10_000
_NUM_RE = re.compile(r"^-?\d+(?:\.\d+)?$")
# Minimum length for a valid *x* / _x_ italics pair (markers must wrap content).
_ITALIC_MIN_LEN = 2


def _clean_cell_text(cell_text: str) -> str:
    """Strip markdown artifacts that render literally in Slack raw_text cells.

    Removes bold markers (``**x**`` → ``x``), italics (``*x*``/``_x_``), inline
    code backticks, and leading/trailing whitespace. Leaves everything else
    (unicode emoji, shortcodes, links) untouched.
    """
    cleaned = cell_text.strip()
    cleaned = cleaned.replace("**", "")
    cleaned = cleaned.replace("`", "")
    # Single-asterisk italics: only when they wrap non-empty content
    # (avoid stripping lone asterisks used as literal text).
    for marker in ("*", "_"):
        if (
            cleaned.startswith(marker)
            and cleaned.endswith(marker)
            and len(cleaned) > _ITALIC_MIN_LEN
        ):
            cleaned = cleaned[1:-1]
            break
    return cleaned


def _table_cell(cell_text: str) -> dict:
    """One table cell: raw_number for numerics, raw_text otherwise.

    Empty cells become a single space — Slack's raw_text schema requires
    ``minLength: 1``, and an empty string makes the whole message
    ``invalid_blocks`` (markdown tables routinely have empty header/body
    cells, and ``_parse_table`` pads short rows with "" too).
    """
    stripped = _clean_cell_text(cell_text)
    if not stripped:
        return {"type": "raw_text", "text": " "}
    if _NUM_RE.match(stripped):
        return {"type": "raw_number", "value": float(stripped), "text": stripped}
    return {"type": "raw_text", "text": stripped}


def markdown_to_table_block(block: str) -> dict | None:
    """Convert one markdown table block into a Slack native table block dict.

    Returns None when the table exceeds Slack's limits (too many rows/cols
    or characters) — the caller should fall back to the image renderer.
    """
    rows, aligns = _parse_table(block)
    if not rows or len(rows) > _TABLE_MAX_ROWS or len(rows[0]) > _TABLE_MAX_COLS:
        return None
    total_chars = sum(len(c) for r in rows for c in r)
    if total_chars > _TABLE_MAX_CHARS:
        return None
    return {
        "type": "table",
        "rows": [[_table_cell(c) for c in r] for r in rows],
        "column_settings": [{"align": a} for a in aligns],
    }


async def _post_native_tables(
    client, channel_id: str, blocks: list[str]  # noqa: ANN001
) -> bool:
    """Post each detected table as a native Slack table block message.

    Slack allows only ONE table block per message, so each table is its own
    chat.postMessage. Returns True if all tables were posted; False when any
    table exceeded native limits (caller should fall back to PNG).
    """
    table_dicts = [markdown_to_table_block(b) for b in blocks]
    if any(t is None for t in table_dicts):
        return False  # at least one table exceeds native limits — fall back

    from . import purge

    for table in table_dicts:
        ts = await safe_post(
            client,
            channel=channel_id,
            text="Table",
            blocks=[table],
        )
        if ts:
            purge.record(channel_id, ts, kind="answer")
    return True


# Fallback text per message (notification preview only — Slack caps the
# ``text`` field at 12k chars; keep it short).
_FALLBACK_TEXT_LIMIT = 2900


def split_into_table_messages(text: str) -> list[tuple[list[dict], str]] | None:
    """Split *text* at markdown-table boundaries into one-message-per-table.

    Each returned message carries at most ONE native table block (Slack's
    limit) plus the section-formatted text that preceded it. Text after the
    last table becomes its own final message. Long text segments are chunked
    into multiple section blocks by ``to_blocks`` — so a long multi-table
    answer splits cleanly at the table boundaries.

    Returns a list of ``(blocks, fallback_text)`` tuples, or None when
    in-place rendering doesn't apply (no table found, a table exceeds the
    native limits, or a table doesn't appear verbatim in *text* — the
    offer-button fallback covers those).
    """
    tables = find_table_blocks(text)
    if not tables:
        return None
    # Any oversized table → let the offer-button flow handle it (PNG render).
    table_dicts = [markdown_to_table_block(t) for t in tables]
    if any(t is None for t in table_dicts):
        return None

    messages: list[tuple[list[dict], str]] = []
    cursor = 0
    for table_str, table in zip(tables, table_dicts, strict=True):
        idx = text.find(table_str, cursor)
        if idx < 0:
            return None  # table text not found verbatim — bail
        pre = text[cursor:idx]
        blocks: list[dict] = []
        if pre.strip():
            pre_blocks, _ = to_blocks(pre)
            blocks.extend(pre_blocks)
        blocks.append(table)
        segment = (pre + table_str).strip()
        messages.append((blocks, segment[:_FALLBACK_TEXT_LIMIT]))
        cursor = idx + len(table_str)
    trailing = text[cursor:]
    if trailing.strip():
        tail_blocks, _ = to_blocks(trailing)
        messages.append((tail_blocks, trailing[:_FALLBACK_TEXT_LIMIT]))
    return messages or None
