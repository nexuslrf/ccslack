"""Minimal shell-output capture for skeleton-scope shell sessions.

ccgram has a ~1,400-LOC prompt-marker pipeline; this is the bare-bones version
that works for short, non-interactive commands. After each ``send_keys`` to a
shell-provider window we:

  1. Snapshot pane scrollback **before** the keypress (cached per-window).
  2. Send the keys (the inbound handler does this).
  3. Wait ``CAPTURE_DELAY_SECONDS`` for the shell to produce output.
  4. Re-capture, diff against the snapshot, ANSI-strip + trim.
  5. Drop the echoed command line and the trailing shell prompt so the post
     is just *the output*.
  6. Render as ``> <command>`` header + fenced code block.

Limitations (documented in AGENT.md):

  * Long-running commands finish after the capture window — output is lost
    until the next send_keys.
  * Interactive commands (vim, less, ssh) tear up the diff entirely.
  * No prompt-marker → can't reliably detect command boundaries; we just
    diff the visible scrollback.
"""

from __future__ import annotations

import asyncio
import re
import structlog
from typing import TYPE_CHECKING

from ..session import session_manager
from ..tmux_manager import tmux_manager
from ..utils import task_done_callback

if TYPE_CHECKING:
    from ..slack_client import SlackClient

logger = structlog.get_logger()

# How long to wait before re-capturing. Tuned for sub-second commands; longer
# commands need a future port of ccgram's prompt-marker pipeline.
CAPTURE_DELAY_SECONDS = 0.9

# Per-window snapshot of pane text taken right before the last send_keys.
_pre_send_snapshot: dict[str, str] = {}
# Per-window command text sent on the last send_keys — we use it to (a) strip
# the echo from the captured output and (b) prefix the post with "> <cmd>".
_pending_command: dict[str, str] = {}

# ANSI CSI + OSC stripper. The shell pane is captured with -e so colour codes
# come through; we drop them before posting since Slack code blocks don't render
# colour anyway.
_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]|\x1b\][^\x07]*\x07")

# Glyphs commonly used as the last character of a shell prompt (PS1).
_PROMPT_TAIL_CHARS = frozenset("$#❯>»λ")
# Characters that, when present somewhere in the line, raise our confidence
# that this is actually a shell prompt rather than user output ending with
# coincidence in $/#. Conservative — keeps us from stripping lines like
# ``echo "got 42 #issues"``.
_PROMPT_SANITY_CHARS = frozenset(":/~@")


def is_shell_window(window_id: str) -> bool:
    """Return True when ``window_id`` is bound to the shell provider."""
    view = session_manager.view_window(window_id)
    return bool(view and view.provider_name == "shell")


async def snapshot_pre_send(window_id: str, command: str = "") -> None:
    """Cache the pane scrollback + command text for diffing after send_keys."""
    text = await tmux_manager.capture_pane_scrollback(window_id, history=200)
    _pre_send_snapshot[window_id] = text or ""
    _pending_command[window_id] = command


def schedule_capture(
    client: SlackClient,
    channel_id: str,
    window_id: str,
) -> None:
    """Spawn a background task that captures + posts shell output."""
    task = asyncio.create_task(
        _capture_and_post(client, channel_id, window_id),
        name=f"ccslack-shell-capture-{window_id}",
    )
    task.add_done_callback(task_done_callback)


async def _capture_and_post(
    client: SlackClient,
    channel_id: str,
    window_id: str,
) -> None:
    """Wait briefly, capture pane, diff against snapshot, post the new lines."""
    await asyncio.sleep(CAPTURE_DELAY_SECONDS)
    after = await tmux_manager.capture_pane_scrollback(window_id, history=200)
    if not after:
        _pending_command.pop(window_id, None)
        return
    before = _pre_send_snapshot.pop(window_id, "")
    command = _pending_command.pop(window_id, "").strip()
    raw_new = _diff_new_lines(before, after)
    cleaned = _ANSI_RE.sub("", raw_new)
    output = _strip_trailing_prompt(cleaned)
    output = _strip_echoed_command(output, command).rstrip()

    body = _format_body(command, output)
    if not body:
        return

    # Lazy: pull the sender lazily to avoid a top-level cycle with the
    # bootstrap import path.
    from ..slack_sender import safe_post

    await safe_post(client, channel=channel_id, text=body)


def _diff_new_lines(before: str, after: str) -> str:
    """Return the suffix of ``after`` that wasn't in ``before``.

    Both strings are pane snapshots — ``after`` strictly extends ``before`` in
    the common case (new prompt + command + output appended). Falls back to
    "last 20 lines of after" when the snapshot prefix doesn't match (e.g.
    ``clear`` was issued, scrollback rotated, etc.).
    """
    if not before:
        return after
    if after.startswith(before):
        return after[len(before) :]
    # Snapshot rotated — show the tail.
    return "\n".join(after.splitlines()[-20:])


def _looks_like_prompt(line: str) -> bool:
    """Heuristic: a non-empty line that ends in a known prompt glyph and looks
    pathy/user-y enough to be a PS1 line rather than coincidental output."""
    stripped = line.rstrip()
    if not stripped:
        return False
    if stripped[-1] not in _PROMPT_TAIL_CHARS:
        return False
    # The body of the prompt almost always contains a path / user / host hint.
    return any(c in stripped for c in _PROMPT_SANITY_CHARS)


def _strip_trailing_prompt(text: str) -> str:
    """Remove trailing shell-prompt lines (and blank lines) from a capture."""
    lines = text.splitlines()
    while lines and (not lines[-1].strip() or _looks_like_prompt(lines[-1])):
        lines.pop()
    return "\n".join(lines)


def _strip_echoed_command(text: str, command: str) -> str:
    """Drop the first non-empty line iff it equals the echoed ``command``."""
    if not command:
        return text
    lines = text.splitlines()
    # Skip leading blank lines.
    idx = 0
    while idx < len(lines) and not lines[idx].strip():
        idx += 1
    if idx < len(lines) and lines[idx].strip() == command:
        del lines[idx]
    return "\n".join(lines)


def _format_body(command: str, output: str) -> str:
    """Render the Slack post for a captured shell command."""
    header = f"> `{command}`" if command else ""
    if output:
        body = f"```\n{output}\n```"
        return f"{header}\n{body}" if header else body
    # No output — still show the command echo so the user knows it ran.
    return f"{header} _(no output)_" if header else ""


__all__ = [
    "CAPTURE_DELAY_SECONDS",
    "is_shell_window",
    "schedule_capture",
    "snapshot_pre_send",
]
