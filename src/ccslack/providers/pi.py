"""Pi coding agent provider — https://pi.dev behind AgentProvider.

Pi is a Node.js-based CLI with JSONL session transcripts (v3 format). With the
cc-thingz hook-runner extension, lifecycle hooks provide instant status signals;
transcript discovery remains the fallback and message source of truth. Discovery
scans ``~/.pi/agent/sessions/`` for the newest transcript whose header ``cwd``
matches the window working directory.

Session path:  ``~/.pi/agent/sessions/--<encoded-cwd>--/<timestamp>_<uuid>.jsonl``

CWD encoding strips leading slashes, replaces ``/``, ``\\``, and ``:`` with
``-``, then wraps with ``--`` delimiters.  The canonical session id lives in
the header line ``{"type":"session","id":"<uuid>","cwd":"...","version":3}``
— the filename prefix is just a timestamp.

Resume strategy: always use ``--session <path>``.  ``--resume`` opens pi's
interactive picker, which ccslack can't drive over ``send_keys``.  Pi accepts
both a transcript path and a (partial) UUID for ``--session``, so we route
everything through it after ``shlex.quote`` for safety.
"""

from __future__ import annotations

import shlex
import time
from pathlib import Path
from typing import Any

from ccslack.providers._jsonl import JsonlProvider, parse_jsonl_line
from ccslack.providers.base import (
    AgentMessage,
    DiscoveredCommand,
    ProviderCapabilities,
    SessionStartEvent,
)
from ccslack.providers.pi_discovery import _PI_TELEGRAM_BUILTINS, discover_pi_commands
from ccslack.providers.pi_format import (
    Pending,
    extract_text,
    normalize_pending,
    parse_assistant,
    parse_bash_execution,
    parse_tool_result,
    parse_user,
    read_session_header,
)


def _pi_sessions_dir() -> Path:
    return Path.home() / ".pi" / "agent" / "sessions"


# Cap transcript age when the pane is dead — guards against picking up an
# unrelated historical transcript for the same cwd.
_STALE_TRANSCRIPT_MAX_AGE_SECS = 120.0
_MIN_TIME_PARTS = 3

# How many recent session files to inspect when searching for a cwd match.
_DISCOVERY_SCAN_LIMIT = 20


def encode_cwd_dirname(cwd: str) -> str:
    """Encode a working directory into pi's session subdirectory name.

    Matches pi's own encoding: strip leading slash, then replace ``/``, ``\\``
    and ``:`` with ``-``, then wrap with ``--``.  Root (``/``) renders as
    ``----`` to stay round-trippable.
    """
    stripped = cwd.lstrip("/\\")
    # Also drop trailing separators so "/tmp/foo/" and "/tmp/foo" collide correctly.
    stripped = stripped.rstrip("/\\")
    encoded = stripped.replace("/", "-").replace("\\", "-").replace(":", "-")
    return f"--{encoded}--"


def _parse_filename_timestamp(ts_str: str) -> float:
    """Parse a pi session filename timestamp into a Unix epoch float.

    Pi filenames use ``2026-08-23T04-48-02-669Z`` format (ISO date +
    dash-separated time with millis). Returns 0.0 on parse failure.
    """
    if not ts_str or not ts_str.endswith("Z"):
        return 0.0
    try:
        date_part, time_part = ts_str[:-1].split("T", 1)
        parts = time_part.split("-")
        if len(parts) < _MIN_TIME_PARTS:
            return 0.0
        if len(parts) > _MIN_TIME_PARTS:
            iso_time = ":".join(parts[:3]) + "." + parts[3]
        else:
            iso_time = ":".join(parts)
        iso = f"{date_part}T{iso_time}"
        from datetime import datetime

        return datetime.fromisoformat(iso).timestamp()
    except (ValueError, OSError):
        return 0.0


def _closest_transcript_to(
    cwd: str, proc_start: float, *, max_delta: float = 60.0
) -> Path | None:
    """Find the transcript created closest to (and within *max_delta* of) *proc_start*."""
    best_path: Path | None = None
    best_delta = float("inf")
    for _mtime, path in _candidate_transcripts(cwd):
        name = path.name
        ts_str = name.split("_", 1)[0] if "_" in name else ""
        file_created = _parse_filename_timestamp(ts_str)
        if file_created <= 0:
            continue
        if file_created < proc_start - 5.0:
            continue
        delta = abs(file_created - proc_start)
        if delta < best_delta:
            best_delta = delta
            best_path = path
    if best_path is None or best_delta > max_delta:
        return None
    return best_path


def _candidate_transcripts(cwd: str) -> list[tuple[float, Path]]:
    """Return ``(mtime, path)`` tuples for this cwd's sessions, newest first."""
    session_dir = _pi_sessions_dir() / encode_cwd_dirname(cwd)
    if not session_dir.is_dir():
        return []
    results: list[tuple[float, Path]] = []
    try:
        for entry in session_dir.iterdir():
            if entry.suffix == ".jsonl" and entry.is_file():
                try:
                    mtime = entry.stat().st_mtime
                except OSError:
                    continue
                results.append((mtime, entry))
    except OSError:
        return []
    results.sort(key=lambda pair: pair[0], reverse=True)
    return results


def _parse_message_entry(
    role: str,
    msg: dict[str, Any],
    pending: Pending,
    timestamp: str | None = None,
) -> tuple[list[AgentMessage], Pending]:
    """Dispatch one envelope's inner ``message`` to the role-specific parser."""
    if role == "user":
        return parse_user(msg, timestamp=timestamp), pending
    if role == "assistant":
        return parse_assistant(msg, pending, timestamp=timestamp)
    if role == "toolResult":
        return parse_tool_result(msg, pending, timestamp=timestamp)
    if role == "bashExecution":
        return parse_bash_execution(msg, timestamp=timestamp), pending
    # branchSummary, compactionSummary, custom → no relay output for v1
    return [], pending


class PiProvider(JsonlProvider):
    """AgentProvider implementation for the pi coding agent CLI."""

    _CAPS = ProviderCapabilities(
        name="pi",
        launch_command="pi",
        supports_hook=True,
        supports_hook_events=True,
        hook_event_types=(
            "SessionStart",
            "Stop",
            "SessionEnd",
            "SubagentStart",
            "SubagentStop",
            "Notification",
        ),
        # The cc-thingz hook-runner is an opt-in extension; when it isn't
        # installed (or doesn't fire SessionStart), the monitor polls
        # discover_transcript() to find the session by cwd — same fallback
        # Codex uses. Hooks remain the fast path when present.
        supports_hookless_discovery=True,
        supports_resume=True,
        supports_continue=True,
        supports_structured_transcript=True,
        supports_incremental_read=True,
        transcript_format="jsonl",
        builtin_commands=tuple(_PI_TELEGRAM_BUILTINS.keys()),
        supports_user_command_discovery=False,
        supports_status_snapshot=False,
        supports_mailbox_delivery=True,
        # Pickers verified by sending each command to a live pi process and
        # inspecting tmux capture-pane output (see PR #93 follow-up).
        tui_picker_commands=frozenset(
            {"model", "login", "fork", "clone", "import", "settings"}
        ),
    )

    _BUILTINS = _PI_TELEGRAM_BUILTINS

    # ── Launch ───────────────────────────────────────────────────────────

    def make_launch_args(
        self,
        resume_id: str | None = None,
        use_continue: bool = False,
    ) -> str:
        """Build CLI args.  Prefers ``--session`` (non-interactive)."""
        if resume_id:
            return f"--session {shlex.quote(resume_id)}"
        if use_continue:
            return "--continue"
        return ""

    # ── Transcript parsing ───────────────────────────────────────────────

    def parse_transcript_line(self, line: str) -> dict[str, Any] | None:
        """Unwrap pi's ``message`` envelope into a flat dict keyed by role.

        For session/model_change/etc. entries the raw dict passes through so
        ``apply_task_entries`` and friends can still inspect them.
        """
        raw = parse_jsonl_line(line)
        if raw is None:
            return None
        if raw.get("type") != "message":
            return raw
        inner = raw.get("message")
        if not isinstance(inner, dict):
            return None
        role = inner.get("role")
        if not isinstance(role, str) or not role:
            return None
        flat: dict[str, Any] = {k: v for k, v in raw.items() if k != "type"}
        flat["type"] = role
        flat["message"] = inner
        return flat

    def parse_transcript_entries(
        self,
        entries: list[dict[str, Any]],
        pending_tools: dict[str, Any],
        cwd: str | None = None,  # noqa: ARG002 — kept for protocol compat
    ) -> tuple[list[AgentMessage], dict[str, Any]]:
        messages: list[AgentMessage] = []
        pending = normalize_pending(pending_tools)

        for entry in entries:
            role = entry.get("type", "")
            if role not in ("user", "assistant", "toolResult", "bashExecution"):
                continue
            inner = entry.get("message")
            if not isinstance(inner, dict):
                continue
            timestamp = entry.get("timestamp")
            batch, pending = _parse_message_entry(role, inner, pending, timestamp)
            messages.extend(batch)

        return messages, dict(pending)

    def is_user_transcript_entry(self, entry: dict[str, Any]) -> bool:
        """Check if this Pi entry is a human turn."""
        entry_type = entry.get("type")
        if entry_type == "user":
            return True
        message = entry.get("message")
        if not isinstance(message, dict):
            return False
        return message.get("role") == "user"

    def parse_history_entry(self, entry: dict[str, Any]) -> AgentMessage | None:
        """Parse a raw or flattened Pi transcript entry for history display."""
        entry_type = entry.get("type")
        if entry_type not in ("user", "assistant", "message"):
            return None

        if entry_type == "message":
            message = entry.get("message")
            if not isinstance(message, dict):
                return None
            role = message.get("role")
            content = message.get("content", "")
        else:
            role = entry_type
            message = entry.get("message")
            content = (
                message.get("content", "")
                if isinstance(message, dict)
                else entry.get("content", "")
            )

        if role not in ("user", "assistant"):
            return None

        text = extract_text(content).strip()
        if not text:
            return None
        timestamp = entry.get("timestamp")
        return AgentMessage(
            text=text,
            role=role,
            content_type="text",
            timestamp=timestamp if isinstance(timestamp, str) else None,
        )

    # `parse_transcript_line` flattens Pi envelopes for monitor reads; raw
    # session resolution still uses the overrides above.

    # ── Discovery ────────────────────────────────────────────────────────

    def discover_transcript(
        self,
        cwd: str,
        window_key: str,
        *,
        max_age: float | None = None,
        exclude: frozenset[str] | None = None,
        min_mtime: float = 0.0,
    ) -> SessionStartEvent | None:
        """Return the newest pi transcript whose header cwd matches.

        ``exclude`` skips sessions already claimed by other windows so
        multiple pi agents in the same cwd each bind to their own session.
        """
        if not cwd:
            return None

        age_limit = (
            _STALE_TRANSCRIPT_MAX_AGE_SECS if max_age is None else float(max_age)
        )
        now = time.time()
        try:
            resolved_target = str(Path(cwd).resolve())
        except OSError:
            return None

        skip = exclude or frozenset()
        for mtime, path in _candidate_transcripts(cwd)[:_DISCOVERY_SCAN_LIMIT]:
            if min_mtime > 0 and mtime < min_mtime:
                continue
            if age_limit > 0 and now - mtime > age_limit:
                break
            header = read_session_header(str(path))
            if not header:
                continue
            try:
                header_cwd = str(Path(header["cwd"]).resolve())
            except OSError:
                continue
            if header_cwd != resolved_target:
                continue
            if header["id"] in skip:
                continue
            return SessionStartEvent(
                session_id=header["id"],
                cwd=header["cwd"],
                transcript_path=str(path),
                window_key=window_key,
            )
        return None

    def resolve_session_transcript(self, session_id: str, cwd: str) -> str | None:
        """Resolve a known pi session id to its transcript file path.

        Scans the cwd's session directory for a file whose name contains the
        session id (pi embeds the uuid in the filename). No age cap — the
        restore/resume flow calls this to pre-register a known session before
        the agent writes its first output, so the monitor's byte offset is
        set at the current file end and all new output is caught.
        """
        if not session_id or not cwd:
            return None
        for _mtime, path in _candidate_transcripts(cwd):
            if session_id in path.name:
                return str(path)
        return None

    def discover_session_for_pane(
        self, cwd: str, tty: str
    ) -> SessionStartEvent | None:
        """Attribute a session to the pi process running on *tty*.

        Pi doesn't expose its session id via cmdline or environ, so we match
        the agent process's start time to transcript creation time — the
        session created closest to (and within 60s of) the process start
        is the right one.
        """
        from .process_detection import get_foreground_pid, get_process_start_time

        if not cwd or not tty:
            return None
        pid = get_foreground_pid(tty)
        if pid <= 0:
            return None
        proc_start = get_process_start_time(pid)
        if proc_start <= 0:
            return None

        best_path = _closest_transcript_to(cwd, proc_start)
        if best_path is None:
            return None

        header = read_session_header(str(best_path))
        if not header:
            return None
        return SessionStartEvent(
            session_id=header["id"],
            cwd=header["cwd"],
            transcript_path=str(best_path),
            window_key="",
        )

    # ── Commands ─────────────────────────────────────────────────────────

    def discover_commands(self, base_dir: str) -> list[DiscoveredCommand]:
        return discover_pi_commands(base_dir)
