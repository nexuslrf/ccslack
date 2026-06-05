"""`/ccslack send <path|glob|substring>` — upload file(s) from cwd to the channel.

Three modes in one command (ported from ccgram's ``/send``):

  * **exact path**   — ``/ccslack send docs/arch.png`` → upload that file.
  * **glob**         — ``/ccslack send *.png`` → fnmatch on filenames.
  * **substring**    — ``/ccslack send arch`` → case-insensitive filename search.

For glob / substring the cwd is walked (depth-capped, excluded dirs pruned).
A single match uploads immediately; multiple matches post an ephemeral picker
(one button per file, plus an "Upload all" button when the count is small).
Files at or above ``_CONFIRM_THRESHOLD_BYTES`` (10 MB) prompt a confirm button
before uploading.

Security: every upload — direct, picked, or bulk — passes the full
``send_security.validate_sendable`` stack (path containment, hidden files,
secret patterns, gitleaks rules). There is no hard upper size limit; large
files are gated by the confirm button instead. Gitignored files are *allowed*
(build artifacts, logs, datasets are commonly gitignored yet worth sending;
secrets are still caught by the hidden-file / secret-pattern / gitleaks checks).
"""

from __future__ import annotations

import contextlib
import fnmatch
import os
import structlog
from pathlib import Path
from typing import TYPE_CHECKING, Any

from slack_sdk.errors import SlackApiError

from ..config import config
from ..session import session_manager
from ..slack_client import BoltSlackClient
from ..thread_router import thread_router
from .send_security import is_excluded_dir, validate_sendable

if TYPE_CHECKING:
    from slack_bolt.async_app import AsyncApp

logger = structlog.get_logger()

# Upload-all is offered only when the match count is at or below this — beyond
# it the picker is the safer default (avoids accidentally flooding a channel).
_UPLOAD_ALL_LIMIT = 10
# Slack actions block holds ≤25 elements; reserve one row for "Upload all".
_MAX_PICKER_BUTTONS = 23
# Max chars of a relative path shown on a picker button before truncation.
_LABEL_MAX = 72
# Files at or above this size prompt a confirm button before uploading; smaller
# files upload straight away. There is no hard upper cap — the confirm step is
# the only gate for large files.
_CONFIRM_THRESHOLD_BYTES = 10 * 1024 * 1024

_IMAGE_SUFFIXES = frozenset(
    {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".svg", ".tiff", ".heic"}
)


def _is_image(path: Path) -> bool:
    return path.suffix.lower() in _IMAGE_SUFFIXES


def _safe_mtime(p: Path) -> float:
    try:
        return p.stat().st_mtime
    except OSError:
        return 0.0


def _safe_relative(path: Path, cwd: Path) -> str:
    try:
        return str(path.resolve().relative_to(cwd.resolve()))
    except ValueError, OSError:
        return str(path)


def _walk_filtered(cwd: Path, depth_limit: int) -> list[Path]:
    """Walk *cwd*, pruning excluded dirs, capped at *depth_limit*."""
    cwd_resolved = cwd.resolve()
    files: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(cwd):
        dirnames[:] = [d for d in dirnames if not is_excluded_dir(d)]
        dir_path = Path(dirpath)
        try:
            rel_depth = len(dir_path.resolve().relative_to(cwd_resolved).parts)
        except ValueError, OSError:
            dirnames[:] = []
            continue
        if rel_depth >= depth_limit:
            dirnames[:] = []
        for filename in filenames:
            files.append(dir_path / filename)
    return files


def _find_files(cwd: Path, pattern: str) -> list[Path]:
    """Resolve *pattern* to a list of sendable files under *cwd*.

    Exact path wins; otherwise glob (``*``/``?``) or case-insensitive substring
    over filenames. Results are security-filtered, mtime-sorted (newest first),
    and capped at ``config.send_max_results``.
    """
    is_glob = "*" in pattern or "?" in pattern

    if not is_glob:
        exact = (cwd / pattern).expanduser()
        if exact.exists() and exact.is_file() and validate_sendable(exact, cwd) is None:
            try:
                rel = exact.resolve().relative_to(cwd.resolve())
            except ValueError, OSError:
                rel = None
            if rel is None or not any(is_excluded_dir(part) for part in rel.parts[:-1]):
                return [exact]

    needle = pattern.lower()

    def _name_matches(name: str) -> bool:
        if is_glob:
            return fnmatch.fnmatch(name, pattern)
        return needle in name.lower()

    results = [
        candidate
        for candidate in _walk_filtered(cwd, config.send_search_depth)
        if candidate.is_file()
        and _name_matches(candidate.name)
        and validate_sendable(candidate, cwd) is None
    ]
    results.sort(key=_safe_mtime, reverse=True)
    return results[: config.send_max_results]


def _resolve_cwd(channel_id: str) -> Path | None:
    """Bound window's cwd as a Path, or None when unresolvable."""
    window_id = thread_router.get_window_for_channel(channel_id)
    if window_id is None:
        return None
    view = session_manager.view_window(window_id)
    cwd_str = (view.cwd if view else "") or ""
    return Path(cwd_str).expanduser() if cwd_str else None


async def handle_send(
    client,  # noqa: ANN001 — Bolt-provided AsyncWebClient
    channel_id: str,
    user_id: str,
    raw_path: str,
) -> None:
    """``/ccslack send <path|glob|substring>`` body."""
    if not raw_path:
        await _ephemeral(
            client,
            channel_id,
            user_id,
            "ccslack: usage `/ccslack send <path|glob|substring>` — e.g. "
            "`send docs/arch.png`, `send *.png`, `send arch`.",
        )
        return

    cwd = _resolve_cwd(channel_id)
    if cwd is None:
        await _ephemeral(
            client,
            channel_id,
            user_id,
            "ccslack: not a session channel (or no remembered cwd).",
        )
        return

    # An exact, existing file path (absolute, or relative to cwd) is uploaded
    # directly — even when it lives under a normally-pruned dir like build/ or
    # dist/. The user named it explicitly; excluded-dir pruning is only meant
    # to keep glob/substring *search* from descending into those trees.
    # Security (containment, secrets, size) is still enforced in _upload_one.
    is_pattern = "*" in raw_path or "?" in raw_path
    candidate = Path(raw_path).expanduser()
    if not candidate.is_absolute():
        candidate = cwd / candidate
    if not is_pattern and candidate.is_file():
        await _upload_one(client, channel_id, user_id, candidate, cwd)
        return

    matches = _find_files(cwd, raw_path)
    if not matches:
        await _ephemeral(
            client,
            channel_id,
            user_id,
            f"ccslack: no sendable files match `{raw_path}` under `{cwd}`.",
        )
        return
    if len(matches) == 1:
        await _upload_one(client, channel_id, user_id, matches[0], cwd)
        return

    await _post_picker(client, channel_id, user_id, matches, cwd)


def _human_size(num_bytes: int) -> str:
    mb = num_bytes / (1024 * 1024)
    if mb >= 1:
        return f"{mb:.1f} MB"
    return f"{num_bytes / 1024:.0f} KB"


async def _upload_one(
    client,  # noqa: ANN001
    channel_id: str,
    user_id: str,
    resolved: Path,
    cwd: Path,
    *,
    confirmed: bool = False,
) -> bool:
    """Validate + upload a single file. Returns success.

    For files at or above ``_CONFIRM_THRESHOLD_BYTES`` and not yet
    ``confirmed``, posts an ephemeral confirm button instead of uploading and
    returns False (the click re-enters here with ``confirmed=True``).
    """
    try:
        resolved = resolved.resolve()
    except OSError as exc:
        await _ephemeral(client, channel_id, user_id, f"ccslack: bad path: {exc}")
        return False
    if not resolved.exists() or not resolved.is_file():
        await _ephemeral(
            client, channel_id, user_id, f"ccslack: not a file: `{resolved}`"
        )
        return False

    reason = validate_sendable(resolved, cwd)
    if reason:
        await _ephemeral(client, channel_id, user_id, f"ccslack: refused — {reason}")
        return False

    size = resolved.stat().st_size
    if not confirmed and size >= _CONFIRM_THRESHOLD_BYTES:
        await _post_size_confirm(client, channel_id, user_id, resolved, cwd, size)
        return False

    rel = _safe_relative(resolved, cwd)
    emoji = ":frame_with_picture:" if _is_image(resolved) else ":outbox_tray:"
    bolt_client = BoltSlackClient(client)
    try:
        await bolt_client.files_upload_v2(
            channel=channel_id,
            file=str(resolved),
            filename=resolved.name,
            title=rel,
            initial_comment=f"{emoji} `{rel}` ({resolved.stat().st_size} bytes)",
        )
        return True
    except SlackApiError as exc:
        error = exc.response.get("error") if exc.response else str(exc)
        await _ephemeral(
            client, channel_id, user_id, f"ccslack: upload failed — `{error}`"
        )
        return False


async def _post_picker(
    client,  # noqa: ANN001
    channel_id: str,
    user_id: str,
    matches: list[Path],
    cwd: Path,
) -> None:
    """Post an ephemeral picker: one button per match (+ optional Upload all)."""
    shown = matches[:_MAX_PICKER_BUTTONS]
    buttons: list[dict[str, Any]] = []
    for path in shown:
        rel = _safe_relative(path, cwd)
        emoji = ":frame_with_picture:" if _is_image(path) else ":page_facing_up:"
        label = rel if len(rel) <= _LABEL_MAX else "…" + rel[-(_LABEL_MAX - 1) :]
        buttons.append(
            {
                "type": "button",
                "action_id": f"ccslack_send_pick:{path.resolve()}",
                "text": {"type": "plain_text", "text": f"{emoji} {label}"[:75]},
                "value": str(path.resolve()),
            }
        )

    blocks: list[dict[str, Any]] = [
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": (
                    f":mag: *{len(matches)} match(es)* under `{cwd}` — pick one"
                    + (
                        f" (showing first {len(shown)})"
                        if len(matches) > len(shown)
                        else ""
                    )
                    + ":"
                ),
            },
        }
    ]
    # Chunk buttons into actions blocks (≤25 elements each; we use ≤23).
    for i in range(0, len(buttons), _MAX_PICKER_BUTTONS):
        blocks.append({"type": "actions", "elements": buttons[i : i + 25]})

    if len(matches) <= _UPLOAD_ALL_LIMIT:
        blocks.append(
            {
                "type": "actions",
                "elements": [
                    {
                        "type": "button",
                        "action_id": "ccslack_send_all",
                        "style": "primary",
                        "text": {
                            "type": "plain_text",
                            "text": f":inbox_tray: Upload all {len(matches)}",
                        },
                        # Value carries newline-joined absolute paths.
                        "value": "\n".join(str(p.resolve()) for p in matches)[:1990],
                    }
                ],
            }
        )

    await _ephemeral(
        client,
        channel_id,
        user_id,
        f"{len(matches)} files match",
        blocks=blocks,
    )


async def _post_size_confirm(
    client,  # noqa: ANN001
    channel_id: str,
    user_id: str,
    resolved: Path,
    cwd: Path,
    size: int,
) -> None:
    """Ephemeral confirm for a large file: ``Upload (X MB)`` / Cancel."""
    rel = _safe_relative(resolved, cwd)
    blocks: list[dict[str, Any]] = [
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": (
                    f":warning: `{rel}` is *{_human_size(size)}* — larger than "
                    f"{_CONFIRM_THRESHOLD_BYTES // (1024 * 1024)} MB. Upload it?"
                ),
            },
        },
        {
            "type": "actions",
            "elements": [
                {
                    "type": "button",
                    "action_id": f"ccslack_send_confirm:{resolved}",
                    "style": "primary",
                    "text": {
                        "type": "plain_text",
                        "text": f":inbox_tray: Upload ({_human_size(size)})",
                    },
                    "value": str(resolved),
                },
                {
                    "type": "button",
                    "action_id": "ccslack_send_cancel",
                    "text": {"type": "plain_text", "text": ":x: Cancel"},
                    "value": "cancel",
                },
            ],
        },
    ]
    await _ephemeral(
        client,
        channel_id,
        user_id,
        f"Confirm upload of {rel} ({_human_size(size)})",
        blocks=blocks,
    )


def register(app: AsyncApp) -> None:
    """Wire the send picker action handlers."""
    import re as _re

    @app.action(_re.compile(r"^ccslack_send_pick:.+$"))
    async def on_pick(ack, body, client) -> None:  # noqa: ANN001
        await ack()
        await _dispatch_pick(body, client)

    @app.action("ccslack_send_all")
    async def on_all(ack, body, client) -> None:  # noqa: ANN001
        await ack()
        await _dispatch_all(body, client)

    @app.action(_re.compile(r"^ccslack_send_confirm:.+$"))
    async def on_confirm(ack, body, client) -> None:  # noqa: ANN001
        await ack()
        await _dispatch_confirm(body, client)

    @app.action("ccslack_send_cancel")
    async def on_cancel(ack, _body, _client) -> None:  # noqa: ANN001
        # Just acknowledge; the ephemeral confirm vanishes for the clicker.
        await ack()


def _action_ctx(body: dict[str, Any]) -> tuple[str, str, Path | None]:
    """Common (user_id, channel_id, cwd) extraction + auth for action handlers.

    Returns ``(user_id, channel_id, cwd)``; ``cwd`` is None when the click is
    unauthorized or the channel has no resolvable cwd (callers should bail).
    """
    user_id = body.get("user", {}).get("id", "")
    channel_id = body.get("channel", {}).get("id", "")
    from .auth import is_authorized

    if not is_authorized(user_id, channel_id) or not channel_id:
        return user_id, channel_id, None
    return user_id, channel_id, _resolve_cwd(channel_id)


async def _dispatch_pick(body: dict[str, Any], client) -> None:  # noqa: ANN001
    user_id, channel_id, cwd = _action_ctx(body)
    if cwd is None:
        return
    path = _picked_value(body, "ccslack_send_pick:")
    if not path:
        return
    await _upload_one(client, channel_id, user_id, Path(path), cwd)


async def _dispatch_confirm(body: dict[str, Any], client) -> None:  # noqa: ANN001
    user_id, channel_id, cwd = _action_ctx(body)
    if cwd is None:
        return
    path = _picked_value(body, "ccslack_send_confirm:")
    if not path:
        return
    # confirmed=True bypasses the size gate; security re-validation still runs.
    await _upload_one(client, channel_id, user_id, Path(path), cwd, confirmed=True)


async def _dispatch_all(body: dict[str, Any], client) -> None:  # noqa: ANN001
    user_id, channel_id, cwd = _action_ctx(body)
    if cwd is None:
        return
    raw = _picked_value(body, "ccslack_send_all")
    paths = [p for p in raw.split("\n") if p.strip()]
    ok = 0
    for p in paths:
        # Bulk is an explicit opt-in, so skip the per-file size confirm.
        if await _upload_one(client, channel_id, user_id, Path(p), cwd, confirmed=True):
            ok += 1
    if ok < len(paths):
        await _ephemeral(
            client,
            channel_id,
            user_id,
            f"ccslack: uploaded {ok}/{len(paths)} files (rest refused/failed).",
        )


def _picked_value(body: dict[str, Any], prefix: str) -> str:
    for action in body.get("actions", []) or []:
        aid = action.get("action_id", "")
        if aid == prefix or aid.startswith(prefix):
            return action.get("value", "")
    return ""


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


__all__ = ["handle_send", "register"]
