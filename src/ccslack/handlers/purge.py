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

import asyncio
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

# Slack chat.delete is Tier 3 (~50/min sustained). A 0.2 s inter-delete gap
# keeps burst bursts under the threshold; the ratelimited retry below handles
# the rare case where a long purge still triggers a 429.
_DELETE_INTERVAL = 0.2

# Cap ledger entries kept per channel (oldest dropped) so purge.json stays small.
_MAX_LEDGER_PER_CHANNEL = 2000

# channel_id -> list of {"ts": str, "thread": str|None, "round": int, "kind": str}
_ledger: dict[str, list[dict[str, Any]]] = {}
# channel_id -> autopurge window in hours (>0 = on).
_autopurge: dict[str, float] = {}
# channel_id -> current conversation round counter.
_round: dict[str, int] = {}
# channel_id -> round number a response purge-button was already posted for
# (so a round with several output messages still shows just ONE button).
_button_posted: dict[str, int] = {}
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
    except OSError, json.JSONDecodeError:
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
    _button_posted.clear()


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
    text: str | None = None,
) -> None:
    """Record a ccslack-posted message so it can be purged later.

    ``file_id`` (for uploads) is also deleted via ``files.delete`` on purge, so
    the underlying file object is removed — not just the message. ``text`` is
    kept for the user-echo entry so purge can annotate it in place rather than
    delete it.
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
    if text is not None:
        entry["text"] = text
    entries = _ledger.setdefault(channel_id, [])
    entries.append(entry)
    if len(entries) > _MAX_LEDGER_PER_CHANNEL:
        del entries[: len(entries) - _MAX_LEDGER_PER_CHANNEL]
    _save()


# ---------------------------------------------------------------------------
# Deletion
# ---------------------------------------------------------------------------


def _retry_after(exc: SlackApiError) -> float:
    """Extract the Retry-After seconds from a ratelimited SlackApiError."""
    with contextlib.suppress(Exception):
        if exc.response and hasattr(exc.response, "headers"):
            return float(exc.response.headers.get("Retry-After", 1))
    return 1.0


async def _delete_one_message(client: SlackClient, channel_id: str, ts: str) -> None:
    """Delete one message; if rate-limited, sleep and retry once."""
    for attempt in range(2):
        try:
            await client.chat_delete(channel=channel_id, ts=ts)
            return
        except SlackApiError as exc:
            error = exc.response.get("error") if exc.response else str(exc)
            if error in ("message_not_found", "already_deleted"):
                return
            if error == "ratelimited" and attempt == 0:
                delay = _retry_after(exc) + 0.5
                logger.debug(
                    "purge: rate limited on chat.delete, retrying in %.1fs", delay
                )
                await asyncio.sleep(delay)
                continue
            logger.debug("purge: chat.delete %s failed: %s", ts, error)
            return


async def _delete_one_file(client: SlackClient, file_id: str) -> None:
    """Delete one file; if rate-limited, sleep and retry once."""
    for attempt in range(2):
        try:
            await client.files_delete(file=file_id)
            return
        except SlackApiError as exc:
            error = exc.response.get("error") if exc.response else str(exc)
            if error in ("file_not_found", "file_deleted"):
                return
            if error == "ratelimited" and attempt == 0:
                delay = _retry_after(exc) + 0.5
                logger.debug(
                    "purge: rate limited on files.delete, retrying in %.1fs", delay
                )
                await asyncio.sleep(delay)
                continue
            logger.debug("purge: files.delete %s failed: %s", file_id, error)
            return


async def _delete_entries(
    client: SlackClient, channel_id: str, entries: list[dict[str, Any]]
) -> int:
    """Delete each entry's message (and its uploaded file). Best-effort count.

    Paces deletions at _DELETE_INTERVAL seconds apart to stay within Slack's
    Tier 3 rate limit; retries once on 429 using the Retry-After header.
    """
    deleted = 0
    for entry in entries:
        ts = entry.get("ts")
        if ts:
            await _delete_one_message(client, channel_id, ts)
        file_id = entry.get("file")
        if file_id:
            await _delete_one_file(client, file_id)
        deleted += 1
        await asyncio.sleep(_DELETE_INTERVAL)
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
    if count is None and since_seconds is None:
        deleted += await _purge_scan_history(client, channel_id)
    return deleted


def _status_preserved_ts(channel_id: str) -> set[str]:
    """Return the ts set that must survive a history scan (the status message)."""
    # Lazy: thread_router / window_store import purge indirectly via session
    # lifecycle — top-level import would create a cycle.
    from ..thread_router import thread_router
    from ..window_state_store import window_store

    preserved: set[str] = set()
    window_id = thread_router.get_window_for_channel(channel_id)
    if window_id:
        state = window_store.window_states.get(window_id)
        if state and state.status_message_ts:
            preserved.add(state.status_message_ts)
    return preserved


async def _delete_thread_replies(
    client: SlackClient, channel_id: str, thread_ts: str, bot_id: str
) -> int:
    """Delete all bot-owned replies in a thread (cursor-paginated).

    ``conversations.replies`` always includes the parent as the first message;
    skip it here since the caller already handled the parent.
    """
    deleted = 0
    cursor: str | None = None
    while True:
        kwargs: dict[str, Any] = {"channel": channel_id, "ts": thread_ts, "limit": 200}
        if cursor:
            kwargs["cursor"] = cursor
        try:
            result = await client.conversations_replies(**kwargs)
        except SlackApiError:
            break
        messages = (result.get("messages") or []) if result else []
        for msg in messages:
            ts = msg.get("ts", "")
            if not ts or ts == thread_ts:
                continue  # skip the parent (always first in the list)
            if msg.get("bot_id") != bot_id:
                continue
            await _delete_one_message(client, channel_id, ts)
            deleted += 1
            await asyncio.sleep(_DELETE_INTERVAL)
        if not result or not result.get("has_more"):
            break
        cursor = (result.get("response_metadata") or {}).get("next_cursor") or None
        if not cursor:
            break
    return deleted


async def _scan_history_page(
    client: SlackClient,
    channel_id: str,
    bot_id: str,
    preserved: set[str],
    cursor: str | None,
) -> tuple[int, str | None]:
    """Fetch one page of history, delete matching bot messages, return (count, next_cursor)."""
    kwargs: dict[str, Any] = {"channel": channel_id, "limit": 200}
    if cursor:
        kwargs["cursor"] = cursor
    result = await client.conversations_history(**kwargs)
    messages = (result.get("messages") or []) if result else []
    deleted = 0
    for msg in messages:
        ts = msg.get("ts", "")
        if not ts or ts in preserved or msg.get("bot_id") != bot_id:
            continue
        await _delete_one_message(client, channel_id, ts)
        deleted += 1
        await asyncio.sleep(_DELETE_INTERVAL)
        # Also sweep any thread replies that conversations.history doesn't return.
        if msg.get("reply_count", 0) > 0:
            deleted += await _delete_thread_replies(client, channel_id, ts, bot_id)
    next_cursor: str | None = None
    if result and result.get("has_more"):
        next_cursor = (result.get("response_metadata") or {}).get("next_cursor") or None
    return deleted, next_cursor


async def _purge_scan_history(client: SlackClient, channel_id: str) -> int:
    """Scan channel history and delete unrecorded bot messages (historical orphans).

    Fetches all messages posted by this bot in the channel and deletes any
    not in the preserved set (the pinned status message). Catches messages
    that predate the ledger or were posted without a record() call.
    """
    try:
        auth = await client.auth_test()
        bot_id = (auth.get("bot_id") or "") if auth else ""
    except SlackApiError:
        return 0
    if not bot_id:
        return 0

    preserved = _status_preserved_ts(channel_id)
    deleted = 0
    cursor: str | None = None
    while True:
        try:
            count, cursor = await _scan_history_page(
                client, channel_id, bot_id, preserved, cursor
            )
            deleted += count
        except SlackApiError:
            break
        if not cursor:
            break
    return deleted


def _ts_age_ok(ts: str, cutoff: float) -> bool:
    try:
        return float(ts) >= cutoff
    except TypeError, ValueError:
        return False


_ECHO_PURGED_NOTE = "\n\n_:wastebasket: Responses purged._"


async def purge_round(client: SlackClient, channel_id: str, round_id: int) -> int:
    """Delete the answer (+ its control button) messages of one round.

    The user's prompt echo is kept (not deleted) but annotated in place with a
    line noting its outputs were purged, so the channel still shows what was
    asked.
    """
    _ensure_loaded()
    ledger = _ledger.get(channel_id, [])
    selected = [
        e
        for e in ledger
        if e["round"] == round_id and e["kind"] in ("answer", "control")
    ]
    deleted = await _delete_entries(client, channel_id, selected)
    _drop_entries(channel_id, {e["ts"] for e in selected})

    if deleted:
        echo = next(
            (e for e in ledger if e["round"] == round_id and e["kind"] == "echo"),
            None,
        )
        if echo and echo.get("text") and _ECHO_PURGED_NOTE not in echo["text"]:
            from ..slack_sender import safe_update

            new_text = echo["text"] + _ECHO_PURGED_NOTE
            with contextlib.suppress(SlackApiError):
                await safe_update(
                    client, channel=channel_id, ts=echo["ts"], text=new_text
                )
            echo["text"] = new_text  # keep idempotent if re-run
            _save()
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


async def delete_file(
    client: SlackClient, channel_id: str, file_id: str, ts: str
) -> None:
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
    _button_posted.pop(channel_id, None)
    if had_ledger or had_auto:
        _save()


async def post_response_button(client: SlackClient, channel_id: str) -> None:
    """Post ONE 'Purge this response' button per round (after the first output).

    A round can emit several output messages; we only offer a single button for
    it (clicking purges the whole round), so the channel doesn't fill with a
    button per message. Recorded as ``control`` so a later purge sweeps it too.
    """
    _ensure_loaded()
    round_id = _round.get(channel_id, 0)
    if _button_posted.get(channel_id) == round_id:
        return  # already offered a button for this round
    _button_posted[channel_id] = round_id
    # Lazy: slack_sender pulls config + formatting helpers.
    from ..slack_sender import safe_post

    ts = await safe_post(
        client,
        channel=channel_id,
        text=":wastebasket: Purge",
        blocks=[
            {
                "type": "actions",
                "block_id": f"ccslack_purge_resp:{round_id}",
                "elements": [
                    {
                        "type": "button",
                        "action_id": "ccslack_purge_response",
                        "style": "danger",
                        "text": {"type": "plain_text", "text": ":wastebasket: Purge"},
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
        text=":wastebasket: Remove",
        blocks=[
            {
                "type": "actions",
                "block_id": "ccslack_file_actions",
                "elements": [
                    {
                        "type": "button",
                        "action_id": "ccslack_remove_file",
                        "style": "danger",
                        "text": {"type": "plain_text", "text": ":wastebasket: Remove"},
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
