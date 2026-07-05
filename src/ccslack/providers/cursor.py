"""Cursor Agent CLI provider — ``cursor-agent`` behind the AgentProvider protocol.

Cursor is unlike the other providers in two ways, which shape this module:

  1. **No hooks.** ccslack cannot install a SessionStart hook, so sessions are
     found by *hookless discovery* (:meth:`discover_transcript`): the monitor
     loop scans ``~/.cursor/chats/<md5(cwd)>/<agentId>/store.db`` for the newest
     chat matching a window's working directory.
  2. **SQLite store, not JSONL.** Each chat is a content-addressed SQLite blob
     store (``store.db``): a ``blobs(id, data)`` table where each row is either a
     JSON message (``{role, content, id}``) or a protobuf DAG node (skipped).
     Messages append in ``rowid`` order, so tailing = reading rows with
     ``rowid > last_offset`` — the "offset" carried by the monitor is the last
     rowid, not a byte position. ``supports_incremental_read=False`` routes reads
     through :meth:`read_transcript_file`, which owns the SQLite query.

Message content is a list of typed parts:
  - assistant: ``{type: "text", text}`` / ``{type: "tool-call", toolCallId,
    toolName, args}`` / ``{type: "redacted-reasoning"}`` (skipped)
  - tool:      ``{type: "tool-result", toolCallId, toolName, result}``

WAL note: Cursor writes with WAL, so committed rows land in ``store.db-wal``
before a checkpoint touches ``store.db``. Discovery therefore points
``transcript_path`` at the ``-wal`` file when present (its mtime advances on
every commit, so the monitor's mtime gate fires); reads strip the suffix and
open the base DB read-only.
"""

import hashlib
import json
from pathlib import Path
import sqlite3
import time
from typing import Any, ClassVar

from ccslack.providers._jsonl import JsonlProvider
from ccslack.providers.base import (
    AgentMessage,
    ProviderCapabilities,
    RESUME_ID_RE,
    SessionStartEvent,
)

# Read-only SQLite connections use a short busy timeout; Cursor holds the
# writer, so a contended read just retries on the next poll.
_SQLITE_TIMEOUT = 1.0
_MAX_TOOL_SUMMARY = 200
_MAX_TOOL_RESULT = 4000
_DISCOVER_MAX_AGE_SECS = 300.0
_WAL_SUFFIXES = ("-wal", "-shm")


def _cursor_chats_root() -> Path:
    """Root of Cursor's per-project chat stores."""
    return Path.home() / ".cursor" / "chats"


def _project_hash(cwd: str) -> str:
    """Cursor keys chat stores by md5 of the resolved workspace path."""
    resolved = str(Path(cwd).expanduser().resolve())
    return hashlib.md5(resolved.encode(), usedforsecurity=False).hexdigest()


def _base_db_path(file_path: str) -> str:
    """Strip a ``-wal``/``-shm`` suffix to get the base ``store.db`` path."""
    for suffix in _WAL_SUFFIXES:
        if file_path.endswith(suffix):
            return file_path[: -len(suffix)]
    return file_path


def _wal_or_db(store: Path) -> str:
    """Prefer the ``-wal`` sibling for a live mtime; fall back to the DB."""
    wal = store.with_name(store.name + "-wal")
    return str(wal) if wal.exists() else str(store)


def _newest_mtime(agent_dir: Path) -> float:
    """Newest mtime across the store DB and its WAL/SHM sidecars."""
    newest = 0.0
    for name in ("store.db", "store.db-wal", "store.db-shm"):
        try:
            mtime = (agent_dir / name).stat().st_mtime
        except OSError:
            continue
        newest = max(newest, mtime)
    return newest


def _decode_blob(data: bytes) -> dict[str, Any] | None:
    """Decode a blob row into a message dict, or None for DAG/binary blobs."""
    try:
        obj = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    if isinstance(obj, dict) and "role" in obj:
        return obj
    return None


def _summarize_tool_args(args: Any) -> str:
    """Short one-line summary of a Cursor tool-call's args."""
    if not isinstance(args, dict):
        return ""
    for key in ("command", "file_path", "path", "query", "pattern", "url"):
        value = args.get(key)
        if isinstance(value, str) and value:
            return value[:_MAX_TOOL_SUMMARY]
    for value in args.values():
        if isinstance(value, str) and value:
            return value[:_MAX_TOOL_SUMMARY]
    return ""


def _assistant_text_parts(content: Any) -> str:
    """Join all visible text parts from an assistant/user content list."""
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    return "".join(
        part.get("text", "")
        for part in content
        if isinstance(part, dict) and part.get("type") == "text"
    )


class CursorProvider(JsonlProvider):
    """AgentProvider implementation for the Cursor Agent CLI (``cursor-agent``)."""

    _CAPS = ProviderCapabilities(
        name="cursor",
        launch_command="cursor-agent",
        supports_hook=False,
        supports_hook_events=False,
        hook_install_managed_by_ccslack=False,
        supports_resume=True,
        supports_continue=True,
        supports_structured_transcript=True,
        # Reads route through read_transcript_file (SQLite), not byte offsets.
        supports_incremental_read=False,
        supports_hookless_discovery=True,
        transcript_format="jsonl",
    )

    _BUILTINS: ClassVar[dict[str, str]] = {}

    def make_launch_args(
        self,
        resume_id: str | None = None,
        use_continue: bool = False,
    ) -> str:
        """Build ``cursor-agent`` args: ``--resume <chatId>`` / ``--continue``."""
        if resume_id:
            if not RESUME_ID_RE.match(resume_id):
                raise ValueError(f"Invalid resume_id: {resume_id!r}")
            return f"--resume {resume_id}"
        if use_continue:
            return "--continue"
        return ""

    def read_transcript_file(
        self, file_path: str, last_offset: int
    ) -> tuple[list[dict[str, Any]], int]:
        """Read new message blobs from a Cursor ``store.db`` after ``last_offset``.

        ``last_offset`` is the last SQLite rowid seen. Returns the decoded JSON
        message dicts (each tagged with ``__rowid``) plus the new max rowid.
        Non-message (protobuf DAG) rows are skipped but still advance the offset
        so they are not rescanned. Opens the base DB read-only so Cursor's own
        writer is never disturbed.
        """
        db_path = _base_db_path(file_path)
        entries: list[dict[str, Any]] = []
        new_offset = last_offset
        try:
            con = sqlite3.connect(
                f"file:{db_path}?mode=ro", uri=True, timeout=_SQLITE_TIMEOUT
            )
        except sqlite3.Error:
            return [], last_offset
        try:
            con.row_factory = sqlite3.Row
            rows = con.execute(
                "SELECT rowid AS rid, data FROM blobs WHERE rowid > ? ORDER BY rowid",
                (last_offset,),
            )
            for row in rows:
                new_offset = row["rid"]
                obj = _decode_blob(row["data"])
                if obj is not None:
                    obj["__rowid"] = row["rid"]
                    entries.append(obj)
        except sqlite3.Error:
            return [], last_offset
        finally:
            con.close()
        return entries, new_offset

    def parse_transcript_entries(
        self,
        entries: list[dict[str, Any]],
        pending_tools: dict[str, Any],
        cwd: str | None = None,  # noqa: ARG002 — protocol signature
    ) -> tuple[list[AgentMessage], dict[str, Any]]:
        """Parse Cursor message blobs into AgentMessages.

        Emits assistant text, tool-call (``tool_use``), and tool-result
        (``tool_result``) messages; skips user/system turns (the user's prompt
        arrived from Slack) and redacted reasoning. Tool ids (``toolCallId``)
        pair a call with its result via ``pending``.
        """
        messages: list[AgentMessage] = []
        pending = dict(pending_tools)
        for entry in entries:
            role = entry.get("role")
            if role == "assistant":
                messages.extend(self._parse_assistant(entry, pending))
            elif role == "tool":
                messages.extend(self._parse_tool(entry, pending))
        return messages, pending

    @staticmethod
    def _parse_assistant(
        entry: dict[str, Any], pending: dict[str, Any]
    ) -> list[AgentMessage]:
        content = entry.get("content")
        if not isinstance(content, list):
            text = _assistant_text_parts(content)
            return (
                [AgentMessage(text=text, role="assistant", content_type="text")]
                if text
                else []
            )
        out: list[AgentMessage] = []
        for part in content:
            if not isinstance(part, dict):
                continue
            ptype = part.get("type")
            if ptype == "text" and part.get("text"):
                out.append(
                    AgentMessage(
                        text=part["text"], role="assistant", content_type="text"
                    )
                )
            elif ptype == "tool-call":
                tool_id = part.get("toolCallId")
                tool_name = part.get("toolName") or "tool"
                if isinstance(tool_id, str) and tool_id:
                    pending[tool_id] = tool_name
                summary = _summarize_tool_args(part.get("args"))
                text = f"**{tool_name}** `{summary}`" if summary else f"**{tool_name}**"
                out.append(
                    AgentMessage(
                        text=text,
                        role="assistant",
                        content_type="tool_use",
                        tool_use_id=tool_id if isinstance(tool_id, str) else None,
                        tool_name=tool_name,
                    )
                )
        return out

    @staticmethod
    def _parse_tool(
        entry: dict[str, Any], pending: dict[str, Any]
    ) -> list[AgentMessage]:
        content = entry.get("content")
        if not isinstance(content, list):
            return []
        out: list[AgentMessage] = []
        for part in content:
            if not isinstance(part, dict) or part.get("type") != "tool-result":
                continue
            tool_id = part.get("toolCallId")
            tool_name = part.get("toolName") or "tool"
            if isinstance(tool_id, str):
                pending.pop(tool_id, None)
            result = part.get("result")
            if not isinstance(result, str) or not result:
                continue
            out.append(
                AgentMessage(
                    text=result[:_MAX_TOOL_RESULT],
                    role="assistant",
                    content_type="tool_result",
                    tool_use_id=tool_id if isinstance(tool_id, str) else None,
                    tool_name=tool_name,
                )
            )
        return out

    def is_user_transcript_entry(self, entry: dict[str, Any]) -> bool:
        """Cursor user turns carry ``role == "user"``."""
        return entry.get("role") == "user"

    def parse_history_entry(self, entry: dict[str, Any]) -> AgentMessage | None:
        """Parse a stored message for history display (user + assistant text)."""
        role = entry.get("role")
        if role not in ("user", "assistant"):
            return None
        text = _assistant_text_parts(entry.get("content"))
        if not text:
            return None
        return AgentMessage(text=text, role=role, content_type="text")

    def discover_transcript(
        self,
        cwd: str,
        window_key: str,
        *,
        max_age: float | None = None,
    ) -> SessionStartEvent | None:
        """Find the newest Cursor chat store for ``cwd``.

        Cursor stores chats at ``~/.cursor/chats/<md5(resolved cwd)>/<agentId>/
        store.db``. Returns the most-recently-written chat (by WAL/DB mtime)
        within ``max_age`` seconds, with ``transcript_path`` pointing at the WAL
        sibling when present so the monitor's mtime gate tracks live commits.
        """
        try:
            resolved_cwd = str(Path(cwd).expanduser().resolve())
        except OSError:
            return None
        chats_root = _cursor_chats_root() / _project_hash(resolved_cwd)
        if not chats_root.is_dir():
            return None

        best: tuple[float, str, Path] | None = None
        try:
            agent_dirs = list(chats_root.iterdir())
        except OSError:
            return None
        for agent_dir in agent_dirs:
            if not agent_dir.is_dir():
                continue
            store = agent_dir / "store.db"
            if not store.is_file():
                continue
            mtime = _newest_mtime(agent_dir)
            if best is None or mtime > best[0]:
                best = (mtime, agent_dir.name, store)

        if best is None:
            return None
        mtime, agent_id, store = best
        age_limit = _DISCOVER_MAX_AGE_SECS if max_age is None else max_age
        if age_limit > 0 and time.time() - mtime > age_limit:
            return None
        return SessionStartEvent(
            session_id=agent_id,
            cwd=resolved_cwd,
            transcript_path=_wal_or_db(store),
            window_key=window_key,
        )

    def detect_from_pane_title(
        self,
        pane_current_command: str,  # noqa: ARG002 — protocol signature
        pane_title: str,  # noqa: ARG002 — protocol signature
    ) -> bool:
        # cursor-agent is detected by its command basename, not pane title.
        return False


# Re-exported for the discovery poller + tests without importing the class.
project_hash = _project_hash
cursor_chats_root = _cursor_chats_root

__all__ = ["CursorProvider", "cursor_chats_root", "project_hash"]
