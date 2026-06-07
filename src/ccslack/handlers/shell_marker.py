"""Prompt-marker-driven shell output relay (ccgram parity).

When a shell session has the ``⌘N⌘`` prompt marker injected (via
``providers.shell_infra.setup_shell_prompt``), this module supersedes the
pane-diff fallback in ``handlers/shell_capture.py``. It runs from the polling
tick — passively, no per-keypress capture task — and:

  1. Captures the pane on every tick (cheap viewport read for change detection).
  2. When the captured text changes AND contains markers, re-captures with
     scrollback for reliable command-echo finding.
  3. Identifies the current command via the marker-paired command-echo line.
  4. Streams new output as it appears, by editing the same Slack message via
     ``chat.update`` until the bare prompt marker re-appears (= command done).
  5. Tags the originating Slack message with ✅ / ❌ based on the exit code
     encoded in the marker.

Pure helpers (extraction, line classification, marker stripping) are easy to
unit-test; the relay path is exercised end-to-end via FakeSlackClient.

Ported from ``ccgram/handlers/shell/shell_capture.py``; Slack-flavored
adaptations:
  * channel-keyed state instead of (user_id, thread_id)
  * ``chat.update`` for live edits
  * ``reactions.add`` (white_check_mark / x) on the Slack message that
    originated the command
"""

from __future__ import annotations

import contextlib
import re
import structlog
from dataclasses import dataclass
from typing import TYPE_CHECKING

from slack_sdk.errors import SlackApiError

from ..providers.shell_infra import has_prompt_marker, match_prompt
from ..session import session_manager
from ..tmux_manager import tmux_manager

if TYPE_CHECKING:
    from ..slack_client import SlackClient

logger = structlog.get_logger()

# ANSI stripper (some captures still carry residual codes after pyte renders).
_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]|\x1b\][^\x07]*\x07")
# Marker glyph that should never appear in output we relay. Stripping it keeps
# the relayed pane clean even if the user pastes content containing ⌘.
_MARKER_GLYPH_RE = re.compile(r"⌘\d+⌘\s?")

# Use viewport for change detection (cheap); only do a scrollback capture
# when the viewport has a marker, to cap polling cost.
_SCROLLBACK_LINES = 500


# ---------------------------------------------------------------- data shapes


@dataclass
class _CommandOutput:
    """Sliced output for a finished command."""

    text: str
    exit_code: int | None = None


@dataclass
class _PassiveOutput:
    """Streaming output for an in-progress (or just-finished) command."""

    command_echo: str
    echo_index: int
    text: str
    exit_code: int | None = None


@dataclass
class _ShellMonitorState:
    """Per-window passive-monitor state."""

    last_text_hash: int = 0
    last_command_echo: str = ""
    last_echo_index: int = -1
    msg_ts: str = ""  # Slack ts of the currently-streaming message
    last_output: str = ""
    exit_code_sent: bool = False
    # Slack message that originated this command (for ✅/❌ reactions).
    slack_user_message_ts: str = ""


_state: dict[str, _ShellMonitorState] = {}


def clear_window(window_id: str) -> None:
    """Drop monitor state for a window (archive / unbind / provider switch)."""
    _state.pop(window_id, None)


def reset_for_testing() -> None:
    """Reset all monitor state. Used by unit tests."""
    _state.clear()


def is_shell_window(window_id: str) -> bool:
    """Return True iff ``window_id`` is bound to the shell provider."""
    view = session_manager.view_window(window_id)
    return bool(view and view.provider_name == "shell")


async def has_marker(window_id: str) -> bool:
    """True iff the prompt marker is present in the bottom of the pane."""
    return await has_prompt_marker(window_id)


def mark_slack_command(
    window_id: str,
    *,
    slack_user_message_ts: str,
) -> None:
    """Record the originating Slack message for ✅/❌ reactions on completion."""
    state = _state.setdefault(window_id, _ShellMonitorState())
    state.slack_user_message_ts = slack_user_message_ts


# ---------------------------------------------------------------- pure helpers


def strip_terminal_glyphs(text: str) -> str:
    """Remove ANSI codes + ``⌘N⌘`` markers from text we relay to Slack."""
    text = _ANSI_RE.sub("", text)
    text = _MARKER_GLYPH_RE.sub("", text)
    return text


def _has_markers_in_tail(rendered_text: str) -> bool:
    """Quick check: any prompt marker in the bottom 10 visible lines?

    Strips leading whitespace because pyte may indent wrapped lines.
    """
    lines = rendered_text.rstrip().splitlines()
    tail = lines[max(0, len(lines) - 10) :]
    return any(match_prompt(line.lstrip()) for line in tail)


def _extract_command_output(text: str) -> _CommandOutput:
    """Extract output for a *finished* command (bare-prompt at the bottom).

    Bottom-up scan for a bare marker (= idle prompt). If found, scan upward
    for the previous marker-with-command (= command echo). Output is the
    inclusive range between them.
    """
    lines = text.rstrip().splitlines()
    if not lines:
        return _CommandOutput(text="")

    scan_start = max(0, len(lines) - 10)
    end_idx = None
    exit_code = None
    for i in range(len(lines) - 1, scan_start - 1, -1):
        m = match_prompt(lines[i])
        if m and not m.trailing_text.strip():
            end_idx = i
            exit_code = m.exit_code
            break

    if end_idx is None:
        return _CommandOutput(text="")

    start_idx = None
    for i in range(end_idx - 1, -1, -1):
        m = match_prompt(lines[i])
        if m and m.trailing_text.strip():
            start_idx = i
            break

    if start_idx is None:
        return _CommandOutput(text="", exit_code=exit_code)

    output_lines = lines[start_idx + 1 : end_idx]
    return _CommandOutput(text="\n".join(output_lines), exit_code=exit_code)


def _find_command_echo(lines: list[str]) -> tuple[str, int] | None:
    """Find the most-recent command-echo line above the bottom bare prompt."""
    scan_start = max(0, len(lines) - 10)
    for i in range(len(lines) - 1, scan_start - 1, -1):
        m = match_prompt(lines[i])
        if m and not m.trailing_text.strip():
            for j in range(i - 1, -1, -1):
                mj = match_prompt(lines[j])
                if mj and mj.trailing_text.strip():
                    return (lines[j], j)
            return None
    return None


def _find_in_progress(lines: list[str]) -> _PassiveOutput | None:
    """Find in-progress output — most-recent marker-with-command + tail."""
    for i in range(len(lines) - 1, -1, -1):
        m = match_prompt(lines[i])
        if m and m.trailing_text.strip():
            output_lines = lines[i + 1 :]
            while output_lines and not output_lines[-1].strip():
                output_lines.pop()
            return _PassiveOutput(
                command_echo=lines[i],
                echo_index=i,
                text="\n".join(output_lines),
            )
    return None


def _extract_passive_output(text: str) -> _PassiveOutput | None:
    """Extract output for both in-progress and just-completed commands.

    Returns None when:
      * the pane is empty / has no markers, OR
      * the shell is idle at a bare prompt with no preceding command echo.

    Otherwise returns a ``_PassiveOutput``:
      * ``exit_code is None`` → command still running
      * ``exit_code is int``  → command completed (bare prompt re-appeared)
    """
    lines = text.rstrip().splitlines()
    if not lines:
        return None

    scan_start = max(0, len(lines) - 10)
    has_bare_prompt = False
    for i in range(len(lines) - 1, scan_start - 1, -1):
        m = match_prompt(lines[i])
        if m and not m.trailing_text.strip():
            has_bare_prompt = True
            break

    if has_bare_prompt:
        completed = _extract_command_output(text)
        echo = _find_command_echo(lines)
        if not completed.text and echo is None:
            return None
        return _PassiveOutput(
            command_echo=echo[0] if echo else "",
            echo_index=echo[1] if echo else -1,
            text=completed.text,
            exit_code=completed.exit_code,
        )

    return _find_in_progress(lines)


def _command_from_echo(echo: str) -> str:
    """Extract the command text from a marker-echo line.

    ``"~/code ⌘0⌘ ls -al"`` → ``"ls -al"`` (wrap mode).
    """
    m = match_prompt(echo)
    return m.trailing_text.strip() if m else echo


# ---------------------------------------------------------------- relay loop


async def check_passive_shell_output(
    client: SlackClient,
    *,
    channel_id: str,
    window_id: str,
) -> None:
    """Polling-tick entrypoint. Captures pane, extracts output, relays diff."""
    rendered = await tmux_manager.capture_pane(window_id, with_ansi=False)
    if not rendered:
        return
    text_hash = hash(rendered)
    state = _state.setdefault(window_id, _ShellMonitorState())
    if text_hash == state.last_text_hash:
        return
    state.last_text_hash = text_hash

    if not _has_markers_in_tail(rendered):
        # No markers visible. Either setup never ran, or it got blown away
        # by ``exec bash`` / profile reload. Reset the streaming state so the
        # pane-diff fallback (handlers/shell_capture.py) can take over.
        if not (state.last_command_echo and state.msg_ts):
            _reset_monitor(state)
        return

    scrollback = await tmux_manager.capture_pane_scrollback(
        window_id, history=_SCROLLBACK_LINES
    )
    if not scrollback:
        return

    passive = _extract_passive_output(scrollback)
    if passive is None:
        if not (state.last_command_echo and state.msg_ts):
            _reset_monitor(state)
        return

    # New command boundary — reset the streaming state for the new echo.
    if (
        passive.command_echo != state.last_command_echo
        or passive.echo_index != state.last_echo_index
    ):
        state.last_command_echo = passive.command_echo
        state.last_echo_index = passive.echo_index
        state.msg_ts = ""
        state.last_output = ""
        state.exit_code_sent = False

    await _relay_passive_output(client, channel_id, state, passive)


async def _relay_passive_output(
    client: SlackClient,
    channel_id: str,
    state: _ShellMonitorState,
    passive: _PassiveOutput,
) -> None:
    """Post or update the streaming Slack message + apply final reaction."""
    # Lazy: pull the sender at call-time to avoid bootstrap cycles.
    from ..slack_sender import safe_post, safe_update

    if passive.text != state.last_output:
        state.last_output = passive.text
        cmd = _command_from_echo(passive.command_echo)
        clean_text = strip_terminal_glyphs(passive.text)
        body = _format(cmd, clean_text, in_progress=passive.exit_code is None)
        if state.msg_ts:
            await safe_update(client, channel=channel_id, ts=state.msg_ts, text=body)
        elif body:
            new_ts = await safe_post(client, channel=channel_id, text=body)
            if new_ts:
                state.msg_ts = new_ts

    # One-shot reaction on the user's source Slack message when exit code lands.
    if (
        passive.exit_code is not None
        and not state.exit_code_sent
        and state.slack_user_message_ts
    ):
        state.exit_code_sent = True
        emoji = "white_check_mark" if passive.exit_code == 0 else "x"
        with contextlib.suppress(SlackApiError):
            await client.reactions_add(
                channel=channel_id,
                name=emoji,
                timestamp=state.slack_user_message_ts,
            )
        # Clear so the same reaction doesn't fire twice on the next tick.
        state.slack_user_message_ts = ""


def _reset_monitor(state: _ShellMonitorState) -> None:
    state.last_command_echo = ""
    state.last_echo_index = -1
    state.msg_ts = ""
    state.last_output = ""
    state.exit_code_sent = False


def _format(command: str, output: str, *, in_progress: bool) -> str:
    """Render the Slack post body for a command + (partial) output."""
    header = f"> `{command}`" if command else ""
    if in_progress and header:
        header += " _(running…)_"
    if output:
        body = f"```\n{output}\n```"
        return f"{header}\n{body}" if header else body
    return f"{header} _(no output)_" if header else ""


__all__ = [
    "_extract_command_output",
    "_extract_passive_output",
    "_find_command_echo",
    "_find_in_progress",
    "check_passive_shell_output",
    "clear_window",
    "has_marker",
    "is_shell_window",
    "mark_slack_command",
    "reset_for_testing",
    "strip_terminal_glyphs",
]
