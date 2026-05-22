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


def to_mrkdwn(text: str) -> str:
    """Convert CommonMark-ish markdown to Slack mrkdwn (best-effort).

    Order matters: links are converted before any other ``*`` munging.
    """
    text = _LINK_RE.sub(r"<\2|\1>", text)
    # Convert CommonMark bold ``**x**`` to Slack bold ``*x*``. Slack mrkdwn does
    # not distinguish bold from italic via the surrounding char (``*`` always
    # bold; ``_`` always italic), so a one-step substitution is enough.
    text = _BOLD_RE.sub(r"*\1*", text)
    return text


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
        blocks.append(_mrkdwn_section(text))
        return blocks, _fallback_text(text)

    cursor = 0
    for match in fences:
        prefix = text[cursor : match.start()]
        if prefix.strip():
            blocks.append(_mrkdwn_section(prefix))
        code = match.group(1)
        if code.strip():
            blocks.append(_code_block(code))
        cursor = match.end()
    suffix = text[cursor:]
    if suffix.strip():
        blocks.append(_mrkdwn_section(suffix))
    return blocks, _fallback_text(text)


def _mrkdwn_section(text: str) -> dict[str, Any]:
    """Build a ``section`` block with mrkdwn text, chunked to the 3000-char limit.

    Returns *one* block; longer text is truncated with an ellipsis. Callers
    splitting at the message layer must call ``to_blocks`` per chunk.
    """
    body = to_mrkdwn(text).strip()
    if len(body) > SECTION_TEXT_LIMIT:
        body = body[: SECTION_TEXT_LIMIT - 1] + "…"
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
