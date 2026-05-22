"""`/ccslack send <path>` — upload a file from the session's cwd to the channel.

Walking-skeleton scope:

  * Exact path only (relative to the bound window's cwd or absolute inside cwd).
  * Full security predicate stack via ``send_security.validate_sendable``:
    path containment, hidden files, secret patterns, gitignored, gitleaks
    rules, 50 MB Slack cap.
  * Uploads via ``files_upload_v2`` with ``initial_comment`` carrying the
    relative path.

Glob expansion, substring search and the interactive browser (ccgram's
``send_callbacks``) are intentionally deferred — those add ~700 LOC and
aren't needed to validate the file-delivery story.
"""

from __future__ import annotations

import contextlib
import structlog
from pathlib import Path
from typing import Any

from slack_sdk.errors import SlackApiError

from ..session import session_manager
from ..slack_client import BoltSlackClient
from ..thread_router import thread_router
from .send_security import validate_sendable

logger = structlog.get_logger()


async def handle_send(
    client,  # noqa: ANN001 — Bolt-provided AsyncWebClient
    channel_id: str,
    user_id: str,
    raw_path: str,
) -> None:
    """``/ccslack send <path>`` body."""
    if not raw_path:
        await _ephemeral(
            client,
            channel_id,
            user_id,
            "ccslack: usage `/ccslack send <path>`",
        )
        return

    window_id = thread_router.get_window_for_channel(channel_id)
    if window_id is None:
        await _ephemeral(client, channel_id, user_id, "ccslack: not a session channel.")
        return
    view = session_manager.view_window(window_id)
    cwd_str = (view.cwd if view else "") or ""
    if not cwd_str:
        await _ephemeral(
            client,
            channel_id,
            user_id,
            "ccslack: no remembered cwd for this session.",
        )
        return
    cwd = Path(cwd_str).expanduser()

    candidate = Path(raw_path).expanduser()
    if not candidate.is_absolute():
        candidate = cwd / candidate
    try:
        resolved = candidate.resolve()
    except OSError as exc:
        await _ephemeral(
            client, channel_id, user_id, f"ccslack: bad path `{raw_path}`: {exc}"
        )
        return
    if not resolved.exists() or not resolved.is_file():
        await _ephemeral(
            client, channel_id, user_id, f"ccslack: not a file: `{resolved}`"
        )
        return

    reason = validate_sendable(resolved, cwd)
    if reason:
        await _ephemeral(client, channel_id, user_id, f"ccslack: refused — {reason}")
        return

    rel = _safe_relative(resolved, cwd)
    bolt_client = BoltSlackClient(client)
    try:
        await bolt_client.files_upload_v2(
            channel=channel_id,
            file=str(resolved),
            filename=resolved.name,
            title=rel,
            initial_comment=f":outbox_tray: `{rel}` ({resolved.stat().st_size} bytes)",
        )
    except SlackApiError as exc:
        error = exc.response.get("error") if exc.response else str(exc)
        await _ephemeral(
            client, channel_id, user_id, f"ccslack: upload failed — `{error}`"
        )


def _safe_relative(path: Path, cwd: Path) -> str:
    try:
        return str(path.relative_to(cwd))
    except ValueError:
        return str(path)


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


__all__ = ["handle_send"]
