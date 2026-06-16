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

# Initial pause before the first capture so very fast commands (``pwd``,
# ``echo hi``) get a chance to finish before we start polling.
INITIAL_DELAY_SECONDS = 0.3

# How often to re-capture while waiting for the prompt to come back.
POLL_INTERVAL_SECONDS = 0.5

# Maximum wall-clock wait before we give up and post whatever's there with a
# "still running" note. Covers slow commands like ``du -sh /large/tree`` or
# ``find /`` without hanging the bot forever on tail-like commands.
WAIT_FOR_PROMPT_SECONDS = 30.0

# Legacy alias — kept so existing tests / docs that reference this constant
# don't break.
CAPTURE_DELAY_SECONDS = INITIAL_DELAY_SECONDS

# How many lines of scrollback to capture before/after the keypress. Large
# enough that the pre-send content remains findable as a substring inside the
# post-send capture even when the scrollback window has slid by ~30+ lines
# (e.g. ``rocm-utils`` with multiple GPUs, big build logs).
CAPTURE_HISTORY_LINES = 500

# Cap on how many lines to retain when the pre-send snapshot can't be located
# inside the post-send capture (terminal rotated, ``clear`` was issued, etc.).
# Smaller fallback caps used to truncate medium-sized command output (e.g.
# 8-GPU rocm-utils header) — 50 fits a typical full table.
FALLBACK_TAIL_LINES = 50

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
    text = await tmux_manager.capture_pane_scrollback(
        window_id, history=CAPTURE_HISTORY_LINES
    )
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
    """Poll until the shell prompt returns, then diff + post the new output.

    Instead of the old fixed-delay capture (which truncated any command that
    took longer than ~1 s), we now:

      1. Wait ``INITIAL_DELAY_SECONDS`` so quick commands settle.
      2. Loop: re-capture the pane every ``POLL_INTERVAL_SECONDS``, check
         whether the *original* prompt line (from the pre-send snapshot)
         has re-appeared at the bottom. If yes → command done, stop.
      3. Give up at ``WAIT_FOR_PROMPT_SECONDS`` and post whatever's there
         with a "still running" marker (covers ``tail -f``-style commands).

    Without ccgram's prompt-marker pipeline this is the most reliable
    "command done" signal we have: the shell re-emits its PS1 only when
    the command returns control to the user.
    """
    before = _pre_send_snapshot.pop(window_id, "")
    command = _pending_command.pop(window_id, "").strip()

    await asyncio.sleep(INITIAL_DELAY_SECONDS)
    elapsed = INITIAL_DELAY_SECONDS
    after = ""
    finished = False
    while elapsed < WAIT_FOR_PROMPT_SECONDS:
        after = (
            await tmux_manager.capture_pane_scrollback(
                window_id, history=CAPTURE_HISTORY_LINES
            )
            or ""
        )
        if after and _prompt_returned(before, after):
            finished = True
            break
        await asyncio.sleep(POLL_INTERVAL_SECONDS)
        elapsed += POLL_INTERVAL_SECONDS

    if not after:
        return

    raw_new = _diff_new_lines(before, after, command=command)
    cleaned = _ANSI_RE.sub("", raw_new)
    output = _strip_trailing_prompt(cleaned)
    output = _strip_echoed_command(output, command).rstrip()

    body = _format_body(command, output, still_running=not finished)
    if not body:
        return

    # Lazy: pull the sender lazily to avoid a top-level cycle with the
    # bootstrap import path.
    from ..slack_sender import safe_post

    ts = await safe_post(client, channel=channel_id, text=body)
    # Record so /ccslack purge + autopurge cover shell output too.
    from . import purge

    purge.record(channel_id, ts, kind="answer")


def _prompt_returned(before: str, after: str) -> bool:
    """True iff the pre-send prompt line shows up at the very tail of ``after``.

    Take the last non-empty line of ``before`` (the prompt waiting for input
    when send_keys fired) and find its *last* occurrence in ``after``. The
    prompt has "returned" only when everything after that match is empty
    whitespace — meaning the shell is idle again and waiting for input.

    The naive "needle in last N lines" check returns False positives because
    when the user just typed ``du -sh *`` the first capture shows
    ``prompt$ du -sh *`` on one line — the needle is in there, but the
    command is still running. The tail-empty check eliminates that.
    """
    if not before or not after:
        return False
    before_lines = [line for line in before.splitlines() if line.strip()]
    if not before_lines:
        return False
    needle = before_lines[-1].rstrip()
    if not needle:
        return False
    idx = after.rfind(needle)
    if idx < 0:
        return False
    # If the only thing after the last prompt occurrence is whitespace, the
    # shell is idle. If a command echo / output follows, the command is
    # still running.
    return after[idx + len(needle) :].strip() == ""


def _diff_new_lines(before: str, after: str, command: str = "") -> str:
    """Return the suffix of ``after`` that wasn't in ``before``.

    Capture happens both before and after ``send_keys``, then we diff. The
    captures come from ``tmux capture-pane -p -J -S -N`` whose window is
    pinned to the current pane bottom; as new output streams in, the bottom
    moves down and the window slides too. That means ``after`` rarely starts
    with ``before`` verbatim — they overlap at some non-zero offset.

    Resolution ladder, most precise first:

    1. **Exact prefix** — perfect-match fast path for trivial cases.
    2. **Substring rfind of ``before`` in ``after``** — the captured
       pre-send content exists somewhere inside ``after``; everything after
       it is the new content. Handles the sliding-window case where
       ``after.startswith(before)`` is False but the prefix still appears.
    3. **Command-text anchor** — if neither match works, look for the
       echoed command line itself. Output starts immediately after that.
    4. **Last-resort tail** — keep the bottom ``FALLBACK_TAIL_LINES`` lines.
       Bumped from 20 → 50 so multi-GPU table dumps (rocm-utils etc.) don't
       lose their header.
    """
    if not before:
        return after
    if after.startswith(before):
        return after[len(before) :]

    idx = after.rfind(before)
    if idx >= 0:
        return after[idx + len(before) :]

    # Command-text anchor — slice from just after the echoed command.
    if command:
        for needle in (f"\n{command}\n", f"\n{command}", command):
            cmd_idx = after.rfind(needle)
            if cmd_idx >= 0:
                return after[cmd_idx + len(needle) :]

    return "\n".join(after.splitlines()[-FALLBACK_TAIL_LINES:])


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


def _format_body(command: str, output: str, *, still_running: bool = False) -> str:
    """Render the Slack post for a captured shell command.

    ``still_running=True`` means we hit ``WAIT_FOR_PROMPT_SECONDS`` without
    detecting a prompt return — annotate so users know the output is
    truncated and the command is still going.
    """
    header = f"> `{command}`" if command else ""
    if still_running and header:
        header += (
            f" _(still running — capture stopped at {int(WAIT_FOR_PROMPT_SECONDS)}s)_"
        )
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
