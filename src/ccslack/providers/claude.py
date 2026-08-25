"""Claude Code provider — wraps existing modules behind AgentProvider protocol.

Delegates to hook.py, transcript_parser.py, terminal_parser.py, and
cc_commands.py without changing any behavior. This is a thin adapter layer
that translates between the provider protocol and existing module APIs.
"""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any, cast

import structlog

from ccslack.cc_commands import CC_BUILTINS
from ccslack.providers.base import UUID_RE
from ccslack.providers.base import (
    AgentMessage,
    ContentType,
    DiscoveredCommand,
    MessageRole,
    ProviderCapabilities,
    SessionStartEvent,
    StatusUpdate,
)

from ccslack.terminal_parser import (
    extract_bash_output,
    extract_interactive_content,
    find_chrome_boundary,
    format_status_display,
    parse_status_block,
)
from ccslack.transcript_parser import TranscriptParser
from ccslack.utils import read_cwd_from_jsonl

_log = structlog.get_logger(__name__)

# ── Hookless discovery fallback ──────────────────────────────────────────
# Claude's SessionStart hook is the fast path, but in-TUI branching (/fork,
# /branch) creates a new session without firing a hook. When the tracked
# transcript goes stale, the monitor falls back to discover_transcript() to
# find the newest active transcript for the same cwd.

# Cap transcript age when scanning — guards against picking up an old
# historical transcript for the same cwd.
_STALE_TRANSCRIPT_MAX_AGE_SECS = 120.0
# How many recent files to inspect when searching for a cwd match.
_DISCOVERY_SCAN_LIMIT = 20


def _encode_cwd_dirname(cwd: str) -> str:
    """Encode a cwd into Claude's project dir name (``/`` → ``-``)."""
    return cwd.replace("/", "-")


def _extract_uuid_flag(cmdline: str, flag: str) -> str:
    """Extract a UUID value following *flag* from a cmdline string."""
    import shlex

    try:
        tokens = shlex.split(cmdline)
    except ValueError:
        return ""
    for i, tok in enumerate(tokens):
        if tok == flag and i + 1 < len(tokens):
            value = tokens[i + 1]
            if UUID_RE.match(value):
                return value
        # Also handle --flag=UUID
        if tok.startswith(f"{flag}="):
            value = tok[len(flag) + 1 :]
            if UUID_RE.match(value):
                return value
    return ""


def _extract_path_flag(cmdline: str, flag: str) -> str:
    """Extract a file path value following *flag* from a cmdline string."""
    import shlex

    try:
        tokens = shlex.split(cmdline)
    except ValueError:
        return ""
    for i, tok in enumerate(tokens):
        if tok == flag and i + 1 < len(tokens):
            return tokens[i + 1]
        if tok.startswith(f"{flag}="):
            return tok[len(flag) + 1 :]
    return ""


def _candidate_transcripts(project_dir: Path) -> list[tuple[float, Path]]:
    """Return ``(mtime, path)`` tuples for *.jsonl in *project_dir*, newest first."""
    results: list[tuple[float, Path]] = []
    try:
        for entry in project_dir.iterdir():
            if entry.suffix != ".jsonl" or not entry.is_file():
                continue
            try:
                results.append((entry.stat().st_mtime, entry))
            except OSError:
                continue
    except OSError:
        return []
    results.sort(reverse=True)
    return results


def _match_transcript(
    fpath: Path, resolved_cwd: str, window_key: str
) -> SessionStartEvent | None:
    """Check one transcript file for a cwd match; return a SessionStartEvent."""
    session_id = fpath.stem
    if not UUID_RE.match(session_id):
        return None
    file_cwd = read_cwd_from_jsonl(fpath)
    if not file_cwd:
        return None
    try:
        if str(Path(file_cwd).resolve()) != resolved_cwd:
            return None
    except OSError:
        return None
    return SessionStartEvent(
        session_id=session_id,
        cwd=file_cwd,
        transcript_path=str(fpath),
        window_key=window_key,
    )

_MODE_MARKERS: tuple[str, ...] = ("\u23f5\u23f5", "\u23f8")
_MODE_HINTS: tuple[str, ...] = (
    "auto mode",
    "auto-accept",
    "accept edits",
    "plan mode",
    "bypass permissions",
    "yolo",
    "auto-approve",
)
_MODE_SHORT_LABELS: tuple[tuple[str, str], ...] = (
    ("accept edits", "Edit"),
    ("auto-accept", "Edit"),
    ("plan", "Plan"),
    ("auto mode", "Auto"),
    ("bypass", "YOLO"),
    ("yolo", "YOLO"),
    ("auto-approve", "YOLO"),
)
_LINE_LIMIT = 80


def _find_mode_line(capture: str) -> str | None:
    lines = capture.splitlines()
    boundary = find_chrome_boundary(lines)
    chrome_lines = lines[boundary + 1 :] if boundary is not None else lines[-20:]
    for line in reversed(chrome_lines):
        stripped = line.strip()
        if not stripped:
            continue
        if any(marker in stripped for marker in _MODE_MARKERS):
            return stripped[:_LINE_LIMIT]
    for line in reversed(lines[-25:]):
        stripped = line.strip()
        if not stripped:
            continue
        if any(hint in stripped.lower() for hint in _MODE_HINTS):
            return stripped[:_LINE_LIMIT]
    return None


def _mode_short_label(mode_line: str) -> str | None:
    lower = mode_line.lower()
    for pattern, label in _MODE_SHORT_LABELS:
        if pattern in lower:
            return label
    return None


class ClaudeProvider:
    """AgentProvider implementation for Claude Code CLI."""

    _CAPS = ProviderCapabilities(
        name="claude",
        launch_command="claude",
        supports_hook=True,
        supports_hook_events=True,
        hook_install_managed_by_ccslack=True,
        # Hooks are the fast path, but in-TUI branching (/fork, /branch) creates
        # a new session without firing a hook. Enable hookless discovery as a
        # fallback so the monitor can rebind to the active transcript when
        # the tracked one goes stale.
        supports_hookless_discovery=True,
        hook_event_types=(
            "Notification",
            "Stop",
            "StopFailure",
            "SessionEnd",
            "SubagentStart",
            "SubagentStop",
            "TeammateIdle",
            "TaskCompleted",
        ),
        supports_resume=True,
        supports_continue=True,
        supports_structured_transcript=True,
        transcript_format="jsonl",
        builtin_commands=tuple(CC_BUILTINS.keys()),
        supports_user_command_discovery=True,
        has_yolo_confirmation=True,
        supports_task_tracking=True,
        uses_pyte_status_parsing=True,
        # Commands documented at code.claude.com/docs/en/commands as
        # opening a modal/interactive overlay the user navigates with
        # arrow keys, Enter, and Esc. Sourced from the v2.1.x docs page.
        tui_picker_commands=frozenset(
            {
                "agents",
                "copy",
                "diff",
                "effort",
                "model",
                "permissions",
                "release-notes",
                "rewind",
                "settings",
                "skills",
                "theme",
                "tui",
            }
        ),
    )

    @property
    def capabilities(self) -> ProviderCapabilities:
        return self._CAPS

    def make_launch_args(
        self,
        resume_id: str | None = None,
        use_continue: bool = False,
    ) -> str:
        """Build Claude Code CLI args string for launching or resuming a session."""
        if resume_id:
            if not UUID_RE.match(resume_id):
                raise ValueError(f"Invalid resume_id: {resume_id!r}")
            return f"--resume {resume_id}"
        if use_continue:
            return "--continue"
        return ""

    def read_transcript_file(
        self, file_path: str, last_offset: int
    ) -> tuple[list[dict[str, Any]], int]:
        """Claude uses incremental JSONL reading, not whole-file."""
        msg = "ClaudeProvider uses incremental JSONL reading, not whole-file"
        raise NotImplementedError(msg)

    def parse_transcript_line(self, line: str) -> dict[str, Any] | None:
        """Delegate to TranscriptParser.parse_line."""
        return TranscriptParser.parse_line(line)

    def parse_transcript_entries(
        self,
        entries: list[dict[str, Any]],
        pending_tools: dict[str, Any],
        cwd: str | None = None,
    ) -> tuple[list[AgentMessage], dict[str, Any]]:
        """Parse JSONL entries via TranscriptParser and wrap as AgentMessages."""
        parsed, remaining = TranscriptParser.parse_entries(
            entries, pending_tools, cwd=cwd
        )

        messages = [
            AgentMessage(
                text=e.text,
                role=cast(MessageRole, e.role),
                content_type=cast(ContentType, e.content_type),
                tool_use_id=e.tool_use_id,
                tool_name=e.tool_name,
                timestamp=e.timestamp,
                phase=e.phase,
            )
            for e in parsed
        ]

        return messages, remaining

    def parse_terminal_status(
        self,
        pane_text: str,
        *,
        pane_title: str = "",  # noqa: ARG002
    ) -> StatusUpdate | None:
        """Parse pane text; interactive UI takes precedence over status line."""
        interactive = extract_interactive_content(pane_text)
        if interactive:
            return StatusUpdate(
                raw_text=interactive.content,
                display_label=interactive.name,
                is_interactive=True,
                ui_type=interactive.name,
            )

        raw_status = parse_status_block(pane_text)
        if raw_status:
            headline = raw_status.split("\n", 1)[0]
            return StatusUpdate(
                raw_text=raw_status,
                display_label=format_status_display(headline),
            )

        return None

    def extract_bash_output(self, pane_text: str, command: str) -> str | None:
        return extract_bash_output(pane_text, command)

    def is_user_transcript_entry(self, entry: dict[str, Any]) -> bool:
        return TranscriptParser.is_user_message(entry)

    def parse_history_entry(self, entry: dict[str, Any]) -> AgentMessage | None:
        """Parse a single transcript entry for history display."""
        raw_role = entry.get("type", "assistant")
        if raw_role not in ("user", "assistant"):
            return None
        parsed = TranscriptParser.parse_message(entry)
        if parsed is None or not parsed.text:
            return None
        role = cast(MessageRole, raw_role)
        # "user"/"assistant" message_type maps to "text"; others pass through.
        raw_ct = (
            "text"
            if parsed.message_type in ("user", "assistant")
            else parsed.message_type
        )
        content_type = cast(ContentType, raw_ct)
        return AgentMessage(
            text=parsed.text,
            role=role,
            content_type=content_type,
            tool_name=parsed.tool_name,
            timestamp=TranscriptParser.get_timestamp(entry),
        )

    def requires_pane_title_for_detection(
        self,
        pane_current_command: str,  # noqa: ARG002 — protocol signature
    ) -> bool:
        return False

    def detect_from_pane_title(
        self,
        pane_current_command: str,  # noqa: ARG002 — protocol signature
        pane_title: str,  # noqa: ARG002 — protocol signature
    ) -> bool:
        return False

    def discover_transcript(
        self,
        cwd: str,
        window_key: str,
        *,
        max_age: float | None = None,
        exclude: frozenset[str] | None = None,
        min_mtime: float = 0.0,
    ) -> SessionStartEvent | None:
        """Scan ~/.claude/projects/<encoded-cwd>/ for the newest transcript.

        Claude's SessionStart hook is the fast path, but in-TUI branching
        (/fork, /branch) creates a new session without firing a hook. This
        fallback lets the monitor rebind to the active transcript when the
        tracked one goes stale.

        ``exclude`` skips sessions already claimed by other windows so
        multiple Claude agents in the same cwd each bind to their own.
        """
        if not cwd:
            return None
        try:
            resolved_cwd = str(Path(cwd).resolve())
        except OSError:
            return None
        project_dir = Path.home() / ".claude" / "projects" / _encode_cwd_dirname(cwd)
        if not project_dir.is_dir():
            return None
        age_limit = _STALE_TRANSCRIPT_MAX_AGE_SECS if max_age is None else max_age
        skip = exclude or frozenset()
        for mtime, fpath in _candidate_transcripts(project_dir)[:_DISCOVERY_SCAN_LIMIT]:
            if min_mtime > 0 and mtime < min_mtime:
                continue
            if age_limit > 0 and time.time() - mtime > age_limit:
                break
            event = _match_transcript(fpath, resolved_cwd, window_key)
            if event is not None and event.session_id not in skip:
                return event
        return None

    def resolve_session_transcript(
        self, session_id: str, cwd: str  # noqa: ARG002
    ) -> str | None:
        """Resolve a known Claude session id to its transcript file path.

        Claude stores the session id as the filename stem (UUID), so this is a
        direct lookup. No age cap — used by the restore/resume flow to
        pre-register a known session.
        """
        if not session_id or not UUID_RE.match(session_id):
            return None
        project_dir = Path.home() / ".claude" / "projects" / _encode_cwd_dirname(cwd)
        if not project_dir.is_dir():
            return None
        fpath = project_dir / f"{session_id}.jsonl"
        return str(fpath) if fpath.is_file() else None

    def discover_session_for_pane(
        self, cwd: str, tty: str
    ) -> SessionStartEvent | None:
        """Attribute a session to the Claude process running on *tty*.

        Claude exposes its session id via ``--session-id <uuid>`` or
        ``--resume <path>`` on the command line. Parse it directly for
        exact attribution — no cwd/mtime guessing.
        """
        from .process_detection import get_foreground_pid, get_process_cmdline

        pid = get_foreground_pid(tty)
        if pid <= 0:
            return None
        cmdline = get_process_cmdline(pid)
        if not cmdline:
            return None

        # Try --session-id <uuid>
        session_id = _extract_uuid_flag(cmdline, "--session-id")
        if session_id:
            path = self.resolve_session_transcript(session_id, cwd)
            if path:
                return SessionStartEvent(
                    session_id=session_id, cwd=cwd, transcript_path=path,
                    window_key="",
                )

        # Try --resume <path>
        path = _extract_path_flag(cmdline, "--resume")
        if path and Path(path).is_file():
            return SessionStartEvent(
                session_id=Path(path).stem, cwd=cwd, transcript_path=path,
                window_key="",
            )
        return None

    def discover_commands(self, base_dir: str) -> list[DiscoveredCommand]:
        _ = base_dir
        return [
            DiscoveredCommand(
                name=name,
                description=desc,
                source="builtin",
            )
            for name, desc in CC_BUILTINS.items()
        ]

    def build_status_snapshot(
        self,
        transcript_path: str,
        *,
        display_name: str = "",
        session_id: str = "",
        cwd: str = "",
    ) -> str | None:
        _ = transcript_path, display_name, session_id, cwd
        return None

    def has_output_since(self, transcript_path: str, offset: int) -> bool:
        _ = transcript_path, offset
        return False

    async def scrape_current_mode(
        self,
        window_id: str,
        *,
        capture_fn: Callable[[str], Awaitable[str | None]] | None = None,
    ) -> str | None:
        """Return the short mode label visible in the Claude Code status line.

        ``capture_fn`` is optional and injectable for tests — defaults to
        ``tmux_manager.capture_pane`` so production callers need no changes.
        """
        if capture_fn is not None:
            _fn: Callable[[str], Awaitable[str | None]] = capture_fn
        else:
            # Lazy: tmux_manager imports providers; lazy fallback avoids the
            # cycle when tests inject capture_fn directly.
            from ccslack.tmux_manager import tmux_manager

            _fn = tmux_manager.capture_pane
        try:
            capture = await _fn(window_id)
        except OSError as exc:
            _log.warning("Mode scrape: capture_pane failed %s (%s)", window_id, exc)
            return None
        if not capture:
            return None
        mode_line = _find_mode_line(capture)
        return _mode_short_label(mode_line) if mode_line else None

    async def seed_task_state(
        self,
        window_id: str,
        session_id: str,
        transcript_path: str,
    ) -> None:
        """Seed Claude task-tracking state by reading the full transcript once."""
        # Lazy: aiofiles is heavy; defer until the JSONL reader actually opens a file
        import aiofiles

        # Lazy: claude_task_state ↔ claude provider cycle
        from ccslack.claude_task_state import claude_task_state

        entries: list[dict] = []
        try:
            async with aiofiles.open(transcript_path, "r", encoding="utf-8") as f:
                async for line in f:
                    data = self.parse_transcript_line(line)
                    if data:
                        entries.append(data)
        except OSError:
            _log.exception("seed_task_state: error reading %s", transcript_path)
            return
        claude_task_state.rebuild_from_entries(window_id, session_id, entries)

    def apply_task_entries(
        self,
        window_id: str,
        session_id: str,
        entries: list[dict],
    ) -> None:
        """Apply parsed transcript entries to Claude task-tracking state."""
        # Lazy: claude_task_state ↔ claude provider cycle
        from ccslack.claude_task_state import claude_task_state

        claude_task_state.apply_entries(window_id, session_id, entries)
