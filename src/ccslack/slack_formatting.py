"""Markdown ↔ Slack Block Kit conversion.

Replaces ccgram's ``entity_formatting.py`` (Telegram MessageEntity offsets) with
Slack's ``rich_text`` / ``section`` blocks. The walking-skeleton implementation
is conservative: it emits a single ``section`` block with ``mrkdwn`` text for
short messages, and switches to ``rich_text`` with a ``rich_text_preformatted``
element when fenced code blocks are present.

Public API:
  - ``to_mrkdwn(text)`` — convert generic markdown to Slack mrkdwn syntax
    (best-effort; Slack mrkdwn is *not* CommonMark).
  - ``to_blocks(text)`` — return ``(blocks, fallback_text)`` ready to pass as
    ``blocks=`` / ``text=`` on ``chat_postMessage``.

Slack mrkdwn vs CommonMark — the practical deltas:
  - bold:   ``**text**`` → ``*text*``
  - italic: ``*text*``   → ``_text_``
  - code:   ```text``` stays ```text```
  - links:  ``[label](url)`` → ``<url|label>``
"""

from __future__ import annotations

import re
from typing import Any

# Slack section block text limit (chars). chat_postMessage `text` allows up to
# 40,000; section/mrkdwn each block <=3000.
SECTION_TEXT_LIMIT = 3000
# Hard ceiling for fallback text — safer than 40000.
FALLBACK_TEXT_LIMIT = 12000

_CODE_FENCE_RE = re.compile(r"```(?:[\w-]+)?\n?(.*?)```", re.DOTALL)
_BOLD_RE = re.compile(r"\*\*(.+?)\*\*", re.DOTALL)
_LINK_RE = re.compile(r"\[([^\]]+)\]\(([^)\s]+)\)")
# Markdown ATX header: 1-6 leading hashes + space + text. Slack mrkdwn has no
# header syntax — render the line bold instead (## Sub is not distinguishable
# from # Title, so all levels map to the same bold).
_HEADER_RE = re.compile(r"^#{1,6}[ \t]+(.+?)\s*$", re.MULTILINE)


def to_mrkdwn(text: str) -> str:
    """Convert CommonMark-ish markdown to Slack mrkdwn (best-effort).

    Order matters: links are converted before any other ``*`` munging.
    """
    text = _LINK_RE.sub(r"<\2|\1>", text)
    # Markdown headers (# … ######) → bold lines; requires whitespace after the
    # hashes so ``#hashtag`` or ``#!`` stay literal. Runs BEFORE the bold
    # conversion: an inner ``**x**`` flattens into the header bold (mrkdwn
    # can't nest bold-in-bold).
    text = _HEADER_RE.sub(_header_to_bold, text)
    # Convert CommonMark bold ``**x**`` to Slack bold ``*x*``. Slack mrkdwn does
    # not distinguish bold from italic via the surrounding char (``*`` always
    # bold; ``_`` always italic), so a one-step substitution is enough.
    text = _BOLD_RE.sub(r"*\1*", text)
    return text


def _header_to_bold(match: re.Match) -> str:
    """Header-line replacement: ``# Title`` → ``*Title*`` (``**`` flattened)."""
    content = match.group(1).strip()
    content = content.replace("**", "")
    return f"*{content}*"


def _fallback_text(text: str) -> str:
    """Truncated plain-text fallback for ``text=`` argument."""
    if len(text) <= FALLBACK_TEXT_LIMIT:
        return text
    return text[: FALLBACK_TEXT_LIMIT - 1] + "…"


def to_blocks(text: str) -> tuple[list[dict[str, Any]], str]:
    """Convert a message body to Block Kit blocks + plain-text fallback.

    Returns ``([], text)`` for empty input; callers should treat that as
    "send nothing".
    """
    if not text.strip():
        return [], ""

    fences = list(_CODE_FENCE_RE.finditer(text))
    blocks: list[dict[str, Any]] = []
    if not fences:
        blocks.extend(_section_blocks(text))
        return blocks, _fallback_text(text)

    cursor = 0
    for match in fences:
        prefix = text[cursor : match.start()]
        if prefix.strip():
            blocks.extend(_section_blocks(prefix))
        code = match.group(1)
        if code.strip():
            blocks.extend(_code_blocks(code))
        cursor = match.end()
    suffix = text[cursor:]
    if suffix.strip():
        blocks.extend(_section_blocks(suffix))
    return blocks, _fallback_text(text)


def _chunk(text: str, limit: int) -> list[str]:
    """Split *text* into content-preserving pieces of at most *limit* chars.

    Prefers a newline boundary within the budget so a chunk rarely bisects a
    line; falls back to a hard split. Concatenating the pieces reproduces the
    input exactly (no truncation) — the whole point is that long agent output
    is *carried across blocks*, never dropped.
    """
    chunks: list[str] = []
    remaining = text
    while len(remaining) > limit:
        split = remaining.rfind("\n", 0, limit)
        if split <= 0:
            split = limit
        chunks.append(remaining[:split])
        remaining = remaining[split:]
    if remaining:
        chunks.append(remaining)
    return chunks


_QUOTE_LINE_RE = re.compile(r"^\s*>[ \t]?(.*)$", re.MULTILINE)
# Inline markdown tokens → styled rich_text elements: *bold*, _italic_,
# `code`, ~strike~ (after ** flattening).
_INLINE_MD_RE = re.compile(r"(\*[^*\n]+\*|_[^_\n]+_|`[^`\n]+`|~[^~\n]+~)")


def _split_quote_segments(text: str) -> list[tuple[bool, str]]:
    """Split text into alternating (is_quote, segment) runs.

    A quote run is one or more consecutive lines starting with ``>`` (the
    marker is stripped per line). ``>>>`` (Slack's literal) is left alone —
    only single-marker lines become quotes.
    """
    segments: list[tuple[bool, str]] = []
    current_quote: bool | None = None
    buf: list[str] = []

    def _flush() -> None:
        nonlocal buf, current_quote
        if buf and current_quote is not None:
            segments.append((current_quote, "\n".join(buf)))
        buf = []
        current_quote = None

    for line in text.split("\n"):
        is_quote, stripped = _classify_quote_line(line)
        if current_quote is None or current_quote != is_quote:
            _flush()
            current_quote = is_quote
        buf.append(stripped)
    if buf and current_quote is not None:
        segments.append((current_quote, "\n".join(buf)))
    return segments


def _classify_quote_line(line: str) -> tuple[bool, str]:
    """Return (is_quote, content) for one line (``>>>`` literals stay text)."""
    if line.lstrip().startswith(">>>"):
        return False, line
    m = _QUOTE_LINE_RE.match(line)
    if m:
        return True, m.group(1)
    return False, line


def _inline_md_elements(text: str) -> list[dict[str, Any]]:
    """Convert minimal inline markdown into styled rich_text text elements."""
    # Flatten **x** → *x* first, mirroring to_mrkdwn.
    flattened = _BOLD_RE.sub(r"*\1*", text)
    elements: list[dict[str, Any]] = []
    for token in _INLINE_MD_RE.split(flattened):
        if not token:
            continue
        style_key, body = _token_style(token)
        element: dict[str, Any] = {"type": "text", "text": body}
        if style_key:
            element["style"] = {style_key: True}
        elements.append(element)
    return elements


# Inline-style marker → rich_text style key. A token is styled only when the
# same marker wraps non-empty content (longer than the two markers alone).
_TOKEN_STYLES: tuple[tuple[str, str], ...] = (
    ("*", "bold"),
    ("_", "italic"),
    ("`", "code"),
    ("~", "strike"),
)
_TOKEN_MIN_LEN = 3


def _token_style(token: str) -> tuple[str, str]:
    """Return (style_key, body) for one inline markdown token."""
    for marker, key in _TOKEN_STYLES:
        if (
            token.startswith(marker)
            and token.endswith(marker)
            and len(token) >= _TOKEN_MIN_LEN
        ):
            return key, token[1:-1]
    return "", token


def _quote_block(quote_text: str) -> dict[str, Any]:
    """A rich_text block rendering one quoted run as a blockquote."""
    return {
        "type": "rich_text",
        "elements": [
            {
                "type": "rich_text_quote",
                "elements": _inline_md_elements(quote_text.strip()),
            }
        ],
    }


def _quote_blocks(quote_text: str) -> list[dict[str, Any]]:
    """Quote blocks for a quoted run, chunked to the per-block char limit."""
    stripped = quote_text.strip()
    if not stripped:
        return []
    if len(stripped) <= SECTION_TEXT_LIMIT:
        return [_quote_block(stripped)]
    return [
        _quote_block(chunk) for chunk in _chunk(stripped, SECTION_TEXT_LIMIT)
    ]


def _section_blocks(text: str) -> list[dict[str, Any]]:
    """Section blocks, chunked to the per-block char limit.

    Markdown blockquote lines (``> quoted``) become rich_text quote blocks —
    Block Kit mrkdwn does NOT render ``>`` as a quote, so quotes must be their
    own rich_text blocks to display properly.
    """
    body = to_mrkdwn(text).strip()
    blocks: list[dict[str, Any]] = []
    for is_quote, segment in _split_quote_segments(body):
        if is_quote:
            blocks.extend(_quote_blocks(segment))
        elif segment.strip():
            blocks.extend(
                _mrkdwn_section(c)
                for c in _chunk(segment.strip(), SECTION_TEXT_LIMIT)
            )
    return blocks


def _code_blocks(code: str) -> list[dict[str, Any]]:
    """One or more ``rich_text`` code blocks, chunked to the per-block limit."""
    return [_code_block(chunk) for chunk in _chunk(code, SECTION_TEXT_LIMIT)]


def _mrkdwn_section(text: str) -> dict[str, Any]:
    """Build a single ``section`` block. Input is expected pre-chunked; a hard
    cap guards against Slack's 3000-char limit as a last resort."""
    body = text if len(text) <= SECTION_TEXT_LIMIT else text[: SECTION_TEXT_LIMIT - 1] + "…"
    return {
        "type": "section",
        "text": {"type": "mrkdwn", "text": body},
    }


def _code_block(code: str) -> dict[str, Any]:
    """Build a ``rich_text`` block carrying a single preformatted element."""
    if len(code) > SECTION_TEXT_LIMIT:
        code = code[: SECTION_TEXT_LIMIT - 1] + "…"
    return {
        "type": "rich_text",
        "elements": [
            {
                "type": "rich_text_preformatted",
                "elements": [{"type": "text", "text": code}],
            }
        ],
    }


__all__ = [
    "FALLBACK_TEXT_LIMIT",
    "SECTION_TEXT_LIMIT",
    "to_blocks",
    "to_mrkdwn",
]
