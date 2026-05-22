"""`/ccslack resume` — pick a past Claude session bound to this channel's cwd.

Scans ``~/.claude/projects/*`` (sessions-index.json + bare JSONLs) for sessions
whose ``cwd`` matches the bound window's cwd, presents the most-recent N as
ephemeral Block Kit buttons. Picking one spawns a fresh tmux window with
``claude --resume <session-id>`` and rebinds the current channel to it.
"""

from __future__ import annotations

import contextlib
import json
import structlog
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from slack_sdk.errors import SlackApiError

from ..config import config
from ..providers import resolve_launch_command
from ..session import session_manager
from ..thread_router import thread_router
from ..tmux_manager import tmux_manager
from ..utils import read_session_metadata_from_jsonl
from ..window_state_store import window_store

if TYPE_CHECKING:
    from slack_bolt.async_app import AsyncApp

logger = structlog.get_logger()

_MAX_SESSIONS = 6


@dataclass
class _SessionEntry:
    session_id: str
    summary: str
    mtime: float = 0.0


def scan_sessions_for_cwd(cwd: str) -> list[_SessionEntry]:
    """Return up to ``_MAX_SESSIONS`` past sessions for ``cwd`` (most recent first)."""
    if not config.claude_projects_path.exists() or not cwd:
        return []
    try:
        resolved_cwd = str(Path(cwd).resolve())
    except OSError:
        return []

    candidates: list[tuple[float, _SessionEntry]] = []
    seen_ids: set[str] = set()
    for project_dir in config.claude_projects_path.iterdir():
        if not project_dir.is_dir():
            continue
        index_file = project_dir / "sessions-index.json"
        if index_file.exists():
            _scan_index(index_file, resolved_cwd, seen_ids, candidates)
        _scan_bare_jsonls(project_dir, resolved_cwd, seen_ids, candidates)

    candidates.sort(key=lambda c: c[0], reverse=True)
    return [entry for _, entry in candidates[:_MAX_SESSIONS]]


def _scan_index(
    index_file: Path,
    resolved_cwd: str,
    seen_ids: set[str],
    candidates: list[tuple[float, _SessionEntry]],
) -> None:
    try:
        data = json.loads(index_file.read_text(encoding="utf-8"))
    except json.JSONDecodeError, OSError:
        return
    original_path = data.get("originalPath", "")
    for entry in data.get("entries", []):
        session_id = entry.get("sessionId", "")
        full_path = entry.get("fullPath", "")
        project_path = entry.get("projectPath", original_path)
        if not session_id or not full_path or session_id in seen_ids:
            continue
        try:
            norm_pp = str(Path(project_path).resolve())
        except OSError:
            norm_pp = project_path
        if norm_pp != resolved_cwd:
            continue
        file_path = Path(full_path)
        if not file_path.exists():
            continue
        try:
            mtime = file_path.stat().st_mtime
        except OSError:
            mtime = 0.0
        summary = (
            entry.get("summary", "") or entry.get("firstPrompt", "") or session_id[:12]
        )
        seen_ids.add(session_id)
        candidates.append((mtime, _SessionEntry(session_id, summary, mtime)))


def _scan_bare_jsonls(
    project_dir: Path,
    resolved_cwd: str,
    seen_ids: set[str],
    candidates: list[tuple[float, _SessionEntry]],
) -> None:
    try:
        jsonl_iter = list(project_dir.glob("*.jsonl"))
    except OSError:
        return
    for jsonl_file in jsonl_iter:
        session_id = jsonl_file.stem
        if session_id in seen_ids:
            continue
        file_cwd, summary = read_session_metadata_from_jsonl(jsonl_file)
        if not file_cwd:
            continue
        try:
            norm_cwd = str(Path(file_cwd).resolve())
        except OSError:
            norm_cwd = file_cwd
        if norm_cwd != resolved_cwd:
            continue
        try:
            mtime = jsonl_file.stat().st_mtime
        except OSError:
            mtime = 0.0
        seen_ids.add(session_id)
        candidates.append(
            (mtime, _SessionEntry(session_id, summary or session_id[:12], mtime))
        )


def _label_for(entry: _SessionEntry) -> str:
    short = entry.summary.strip()[:60].replace("\n", " ")
    return short or entry.session_id[:12]


async def handle_resume(
    client,  # noqa: ANN001
    channel_id: str,
    user_id: str,
) -> None:
    """``/ccslack resume`` — list resumable past sessions for this channel."""
    window_id = thread_router.get_window_for_channel(channel_id)
    if window_id is None:
        await _ephemeral(client, channel_id, user_id, "ccslack: not a session channel.")
        return
    view = session_manager.view_window(window_id)
    cwd = (view.cwd if view else "") or ""
    if not cwd:
        await _ephemeral(
            client,
            channel_id,
            user_id,
            "ccslack: no remembered cwd for this session.",
        )
        return

    sessions = scan_sessions_for_cwd(cwd)
    if not sessions:
        await _ephemeral(
            client,
            channel_id,
            user_id,
            f"ccslack: no past sessions found in `{cwd}`.",
        )
        return

    elements: list[dict[str, Any]] = []
    for entry in sessions:
        elements.append(
            {
                "type": "button",
                "action_id": f"ccslack_resume_pick:{entry.session_id}",
                "text": {
                    "type": "plain_text",
                    "text": _label_for(entry)[:75],
                },
                "value": window_id,
            }
        )
    blocks: list[dict[str, Any]] = [
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f":rewind: Pick a session to resume in <#{channel_id}>.",
            },
        },
        # Up to 25 elements per actions block — well under for 6 entries.
        {"type": "actions", "elements": elements},
    ]
    await _ephemeral(
        client,
        channel_id,
        user_id,
        f"Resume picker — {len(sessions)} session(s)",
        blocks=blocks,
    )


def register(app: AsyncApp) -> None:
    import re as _re

    @app.action(_re.compile(r"^ccslack_resume_pick:.+$"))
    async def on_pick(ack, body, client) -> None:  # noqa: ANN001
        await ack()
        user_id = body.get("user", {}).get("id", "")
        channel_id = body.get("channel", {}).get("id", "")
        if not config.is_user_allowed(user_id) or not channel_id:
            return

        action_id = ""
        window_id = ""
        for action in body.get("actions", []) or []:
            aid = action.get("action_id", "")
            if aid.startswith("ccslack_resume_pick:"):
                action_id = aid
                window_id = action.get("value", "")
                break
        if not action_id or not window_id:
            return
        session_id = action_id.split(":", 1)[1]
        await _do_resume(client, channel_id, user_id, window_id, session_id)


async def _do_resume(
    client,  # noqa: ANN001
    channel_id: str,
    user_id: str,
    old_window_id: str,
    session_id: str,
) -> None:
    """Spawn a fresh tmux window with --resume, rebind the channel."""
    view = session_manager.view_window(old_window_id)
    if view is None or not view.cwd:
        await _ephemeral(
            client, channel_id, user_id, "ccslack: lost the original cwd; can't resume."
        )
        return
    provider = view.provider_name or "claude"
    if provider != "claude":
        await _ephemeral(
            client,
            channel_id,
            user_id,
            f"ccslack: resume picker only supports claude (got `{provider}`).",
        )
        return
    launch = resolve_launch_command(provider)
    success, message, _, new_window_id = await tmux_manager.create_window(
        work_dir=view.cwd,
        window_name=thread_router.get_display_name(old_window_id),
        start_agent=True,
        agent_args=f"--resume {session_id}",
        launch_command=launch,
    )
    if not success or not new_window_id:
        await _ephemeral(
            client, channel_id, user_id, f"ccslack: resume failed — {message}"
        )
        return

    # Tear down the old window's state, bind new.
    with contextlib.suppress(OSError, RuntimeError):
        await tmux_manager.kill_window(old_window_id)
    thread_router.unbind_channel(channel_id)
    window_store.remove_window(old_window_id)
    thread_router.bind_channel(
        channel_id,
        new_window_id,
        window_name=thread_router.get_display_name(new_window_id),
    )
    session_manager.set_window_provider(new_window_id, provider, cwd=view.cwd)
    session_manager.set_window_origin(new_window_id, "ccslack_created")

    await _ephemeral(
        client,
        channel_id,
        user_id,
        f":rewind: resumed `{session_id[:12]}…` in tmux `{new_window_id}`",
    )


async def _ephemeral(
    client,  # noqa: ANN001
    channel_id: str,
    user_id: str,
    text: str,
    *,
    blocks: list[dict[str, Any]] | None = None,
) -> None:
    payload: dict[str, Any] = {"channel": channel_id, "user": user_id, "text": text}
    if blocks is not None:
        payload["blocks"] = blocks
    with contextlib.suppress(SlackApiError):
        await client.chat_postEphemeral(**payload)


__all__ = ["handle_resume", "register", "scan_sessions_for_cwd"]
