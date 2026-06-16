"""Delete ccslack's own transcript output from a channel (privacy / cleanup).

ccslack records the message ``ts`` it posts for *transcript* content — agent
answers, tool chains, thinking, user echoes — in a per-channel ledger keyed by
round and thread. That lets it delete, on demand:

  * a whole channel's output (``/ccslack purge``),
  * output older than N hours (``/ccslack autopurge``),
  * one agent answer round (per-response button), or
  * one tool-chain thread (thread Close button).

Only ccslack's *own* transcript posts are recorded, so the pinned status
message, ``/ccslack chat`` threads, toolbar, and live pickers are never
recorded and thus never purged. Slack only lets a bot delete its own messages,
so a user's typed prompts are left untouched regardless.

The ledger persists to ``purge.json`` so ``autopurge`` survives a restart.
"""

from __future__ import annotations

import contextlib
import json
import structlog
import time
from typing import TYPE_CHECKING, Any

from slack_sdk.errors import SlackApiError

from ..config import config
from ..utils import atomic_write_json

if TYPE_CHECKING:
    from slack_bolt.async_app import AsyncApp

    from ..slack_client import SlackClient

logger = structlog.get_logger()

# Cap ledger entries kept per channel (oldest dropped) so purge.json stays small.
_MAX_LEDGER_PER_CHANNEL = 2000

# channel_id -> list of {"ts": str, "thread": str|None, "round": int, "kind": str}
_ledger: dict[str, list[dict[str, Any]]] = {}
# channel_id -> autopurge window in hours (>0 = on).
_autopurge: dict[str, float] = {}
# channel_id -> current conversation round counter.
_round: dict[str, int] = {}
_loaded = False


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------


def _ensure_loaded() -> None:
    global _loaded
    if _loaded:
        return
    _loaded = True
    if not config.purge_file.exists():
        return
    try:
        raw = json.loads(config.purge_file.read_text())
    except (OSError, json.JSONDecodeError):
        return
    led = raw.get("ledger", {})
    if isinstance(led, dict):
        for ch, entries in led.items():
            if not entries:
                continue
            _ledger[str(ch)] = list(entries)
            # Seed the round counter past the highest persisted round so that
            # after a restart a new round can't reuse an old round's number and
            # have the per-response button delete stale messages.
            with contextlib.suppress(ValueError, TypeError):
                highest = max(int(e.get("round", 0)) for e in entries)
                _round[str(ch)] = max(_round.get(str(ch), 0), highest)
    auto = raw.get("autopurge", {})
    if isinstance(auto, dict):
        for ch, hours in auto.items():
            with contextlib.suppress(TypeError, ValueError):
                if float(hours) > 0:
                    _autopurge[str(ch)] = float(hours)


def _save() -> None:
    with contextlib.suppress(OSError):
        atomic_write_json(
            config.purge_file,
            {"ledger": _ledger, "autopurge": _autopurge},
        )


def reset_for_testing() -> None:
    _ledger.clear()
    _autopurge.clear()
    _round.clear()


# ---------------------------------------------------------------------------
# Round + recording
# ---------------------------------------------------------------------------


def current_round(channel_id: str) -> int:
    _ensure_loaded()
    return _round.get(channel_id, 0)


def bump_round(channel_id: str) -> None:
    """Advance the conversation round (called when a fresh user message lands)."""
    _ensure_loaded()
    _round[channel_id] = _round.get(channel_id, 0) + 1


def record(
    channel_id: str,
    ts: str | None,
    *,
    thread_ts: str | None = None,
    kind: str = "answer",
    file_id: str | None = None,
) -> None:
    """Record a ccslack-posted message so it can be purged later.

    ``file_id`` (for uploads) is also deleted via ``files.delete`` on purge, so
    the underlying file object is removed — not just the message.
    """
    if not channel_id or not ts:
        return
    _ensure_loaded()
    entry: dict[str, Any] = {
        "ts": ts,
        "thread": thread_ts,
        "round": _round.get(channel_id, 0),
        "kind": kind,
    }
    if file_id:
        entry["file"] = file_id
    entries = _ledger.setdefault(channel_id, [])
    entries.append(entry)
    if len(entries) > _MAX_LEDGER_PER_CHANNEL:
        del entries[: len(entries) - _MAX_LEDGER_PER_CHANNEL]
    _save()


# ---------------------------------------------------------------------------
# Deletion
# ---------------------------------------------------------------------------


async def _delete_entries(
    client: SlackClient, channel_id: str, entries: list[dict[str, Any]]
) -> int:
    """Delete each entry's message (and its uploaded file). Best-effort count."""
    deleted = 0
    for entry in entries:
        ts = entry.get("ts")
        if ts:
            try:
                await client.chat_delete(channel=channel_id, ts=ts)
            except SlackApiError as exc:
                error = exc.response.get("error") if exc.response else str(exc)
                if error not in ("message_not_found", "already_deleted"):
                    logger.debug("purge: chat.delete %s failed: %s", ts, error)
        file_id = entry.get("file")
        if file_id:
            try:
                await client.files_delete(file=file_id)
            except SlackApiError as exc:
                error = exc.response.get("error") if exc.response else str(exc)
                if error not in ("file_not_found", "file_deleted"):
                    logger.debug("purge: files.delete %s failed: %s", file_id, error)
        deleted += 1
    return deleted


def _drop_entries(channel_id: str, ts_set: set[str]) -> None:
    entries = _ledger.get(channel_id)
    if not entries:
        return
    remaining = [e for e in entries if e["ts"] not in ts_set]
    if remaining:
        _ledger[channel_id] = remaining
    else:
        _ledger.pop(channel_id, None)
    _save()


async def purge(
    client: SlackClient,
    channel_id: str,
    *,
    count: int | None = None,
    since_seconds: float | None = None,
) -> int:
    """Delete recorded output in *channel_id*.

    ``count`` — the most recent N. ``since_seconds`` — posted within the last
    window. Neither — everything recorded. Returns the number deleted.
    """
    _ensure_loaded()
    entries = list(_ledger.get(channel_id, []))
    if not entries:
        return 0
    if since_seconds is not None:
        cutoff = time.time() - since_seconds
        selected = [e for e in entries if _ts_age_ok(e["ts"], cutoff)]
    elif count is not None:
        selected = entries[-count:]
    else:
        selected = entries
    deleted = await _delete_entries(client, channel_id, selected)
    _drop_entries(channel_id, {e["ts"] for e in selected})
    return deleted


def _ts_age_ok(ts: str, cutoff: float) -> bool:
    try:
        return float(ts) >= cutoff
    except (TypeError, ValueError):
        return False


async def purge_round(client: SlackClient, channel_id: str, round_id: int) -> int:
    """Delete the answer (+ its control button) messages of one round."""
    _ensure_loaded()
    selected = [
        e
        for e in _ledger.get(channel_id, [])
        if e["round"] == round_id and e["kind"] in ("answer", "control")
    ]
    deleted = await _delete_entries(client, channel_id, selected)
    _drop_entries(channel_id, {e["ts"] for e in selected})
    return deleted


async def purge_thread(client: SlackClient, channel_id: str, parent_ts: str) -> int:
    """Delete a whole tool-chain thread (parent + all recorded replies)."""
    _ensure_loaded()
    selected = [e for e in _ledger.get(channel_id, []) if e["thread"] == parent_ts]
    if parent_ts not in {e["ts"] for e in selected}:
        selected.append({"ts": parent_ts})  # the parent, even if not recorded
    deleted = await _delete_entries(client, channel_id, selected)
    _drop_entries(channel_id, {e["ts"] for e in selected})
    return deleted


async def delete_file(client: SlackClient, channel_id: str, file_id: str, ts: str) -> None:
    """Remove an uploaded file (and its Remove-button message at *ts*)."""
    await _delete_entries(client, channel_id, [{"ts": ts, "file": file_id}])
    if ts:
        _drop_entries(channel_id, {ts})


# ---------------------------------------------------------------------------
# Autopurge
# ---------------------------------------------------------------------------


def set_autopurge(channel_id: str, hours: float | None) -> None:
    _ensure_loaded()
    if hours and hours > 0:
        _autopurge[channel_id] = float(hours)
    else:
        _autopurge.pop(channel_id, None)
    _save()


def get_autopurge(channel_id: str) -> float:
    _ensure_loaded()
    return _autopurge.get(channel_id, 0.0)


async def sweep(client: SlackClient) -> int:
    """Delete output older than each channel's autopurge window. Returns count."""
    _ensure_loaded()
    if not _autopurge:
        return 0
    now = time.time()
    total = 0
    for channel_id, hours in list(_autopurge.items()):
        cutoff = now - hours * 3600.0
        stale = [
            e
            for e in _ledger.get(channel_id, [])
            if not _ts_age_ok(e["ts"], cutoff)  # older than cutoff
        ]
        if stale:
            total += await _delete_entries(client, channel_id, stale)
            _drop_entries(channel_id, {e["ts"] for e in stale})
    return total


def forget_channel(channel_id: str) -> None:
    """Drop all ledger/autopurge state for a torn-down channel."""
    _ensure_loaded()
    had_ledger = _ledger.pop(channel_id, None) is not None
    had_auto = _autopurge.pop(channel_id, None) is not None
    _round.pop(channel_id, None)
    if had_ledger or had_auto:
        _save()


async def post_response_button(client: SlackClient, channel_id: str) -> None:
    """Post a trailing 'Purge this response' button for the current round.

    Recorded as ``control`` for the round so a later purge / round-purge sweeps
    the button away with the answer it belongs to.
    """
    _ensure_loaded()
    round_id = _round.get(channel_id, 0)
    # Lazy: slack_sender pulls config + formatting helpers.
    from ..slack_sender import safe_post

    ts = await safe_post(
        client,
        channel=channel_id,
        text=":wastebasket: Purge this response?",
        blocks=[
            {
                "type": "actions",
                "block_id": f"ccslack_purge_resp:{round_id}",
                "elements": [
                    {
                        "type": "button",
                        "action_id": "ccslack_purge_response",
                        "style": "danger",
                        "text": {
                            "type": "plain_text",
                            "text": ":wastebasket: Purge this response",
                        },
                        "value": str(round_id),
                    }
                ],
            }
        ],
    )
    record(channel_id, ts, kind="control")


def file_id_from_upload(result: Any) -> str:
    """Best-effort extract the uploaded file id from a files_upload_v2 result."""
    if result is None or not hasattr(result, "get"):
        return ""
    files = result.get("files")
    if (
        isinstance(files, list)
        and files
        and isinstance(files[0], dict)
        and files[0].get("id")
    ):
        return str(files[0]["id"])
    single = result.get("file")
    if isinstance(single, dict) and single.get("id"):
        return str(single["id"])
    return ""


async def post_file_close_button(
    client: SlackClient, channel_id: str, file_id: str
) -> None:
    """Post a 'Remove file' button after an upload (screenshot / send).

    Recorded with the ``file_id`` so a click — or a later purge / autopurge —
    deletes both the button message and the underlying file.
    """
    if not file_id:
        return
    _ensure_loaded()
    from ..slack_sender import safe_post

    ts = await safe_post(
        client,
        channel=channel_id,
        text=":wastebasket: Remove this file?",
        blocks=[
            {
                "type": "actions",
                "block_id": "ccslack_file_actions",
                "elements": [
                    {
                        "type": "button",
                        "action_id": "ccslack_remove_file",
                        "style": "danger",
                        "text": {
                            "type": "plain_text",
                            "text": ":wastebasket: Remove file",
                        },
                        "value": file_id,
                    }
                ],
            }
        ],
    )
    record(channel_id, ts, kind="file", file_id=file_id)


# ---------------------------------------------------------------------------
# Action buttons
# ---------------------------------------------------------------------------


def register(app: AsyncApp) -> None:
    """Wire the per-response purge + tool-thread close buttons."""

    @app.action("ccslack_purge_response")
    async def on_purge_response(ack, body, client) -> None:  # noqa: ANN001
        await ack()
        user_id = body.get("user", {}).get("id", "")
        channel_id = body.get("channel", {}).get("id", "")
        from .auth import is_authorized

        if not is_authorized(user_id, channel_id) or not channel_id:
            return
        value = _action_value(body, "ccslack_purge_response")
        if not value.isdigit():
            return
        await purge_round(client, channel_id, int(value))

    @app.action("ccslack_purge_thread")
    async def on_purge_thread(ack, body, client) -> None:  # noqa: ANN001
        await ack()
        user_id = body.get("user", {}).get("id", "")
        channel_id = body.get("channel", {}).get("id", "")
        from .auth import is_authorized

        if not is_authorized(user_id, channel_id) or not channel_id:
            return
        # The button lives on the thread parent, so the message ts IS the
        # thread's parent ts.
        parent_ts = (body.get("message") or {}).get("ts", "")
        if parent_ts:
            await purge_thread(client, channel_id, parent_ts)

    @app.action("ccslack_remove_file")
    async def on_remove_file(ack, body, client) -> None:  # noqa: ANN001
        await ack()
        user_id = body.get("user", {}).get("id", "")
        channel_id = body.get("channel", {}).get("id", "")
        from .auth import is_authorized

        if not is_authorized(user_id, channel_id) or not channel_id:
            return
        file_id = _action_value(body, "ccslack_remove_file")
        btn_ts = (body.get("message") or {}).get("ts", "")
        if file_id:
            await delete_file(client, channel_id, file_id, btn_ts)


def _action_value(body: dict[str, Any], action_id: str) -> str:
    for action in body.get("actions", []) or []:
        if action.get("action_id") == action_id:
            return action.get("value", "")
    return ""


__all__ = [
    "bump_round",
    "current_round",
    "delete_file",
    "file_id_from_upload",
    "forget_channel",
    "get_autopurge",
    "post_file_close_button",
    "post_response_button",
    "purge",
    "purge_round",
    "purge_thread",
    "record",
    "register",
    "reset_for_testing",
    "set_autopurge",
    "sweep",
]
