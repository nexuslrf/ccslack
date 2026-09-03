"""Status polling coordinator — single background task.

Iterates ``thread_router.channel_bindings`` once per ``config.status_poll_interval``,
reconciling each bound window's status against tmux. See module docstring of
``handlers/polling`` for scope.
"""

from __future__ import annotations

import asyncio
import contextlib
import structlog
import time
from typing import TYPE_CHECKING

from ...config import config
from ...thread_router import thread_router
from ...tmux_manager import tmux_manager
from ...utils import task_done_callback
from ...window_state_store import window_store

if TYPE_CHECKING:
    from ...slack_client import SlackClient

logger = structlog.get_logger()

# How long the agent needs to be quiet before we flip active→idle.
IDLE_DECAY_SECONDS = 5.0

# Auto-toolbar: if the agent hasn't produced output for this long after a
# prompt, auto-open the toolbar (likely waiting for human input/approval).
_HANGING_THRESHOLD_SECS = 120.0  # 2 minutes

# Extended hang: post @channel once after this long.
_EXTENDED_HANG_SECS = 600.0  # 10 minutes

# Track per-window auto-toolbar state: (prompt_sent_at, toolbar_opened, hang_notified)
# prompt_sent_at = monotonic time of the last user message forwarded to the agent
# toolbar_opened = whether the auto-toolbar is currently open
# hang_notified = whether the @channel notification was already sent
_auto_toolbar: dict[str, tuple[float, bool, bool]] = {}

_poll_task: asyncio.Task[None] | None = None

# Track last-seen activity per window_id (monotonic seconds). Updated when
# message_routing posts content for a window.
_last_activity: dict[str, float] = {}


def mark_active(window_id: str) -> None:
    """Bookkeeping: called by message_routing when fresh content arrives."""
    _last_activity[window_id] = time.monotonic()
    # Fresh output = the agent is working. Reset the auto-toolbar hang state
    # but keep the toolbar open if it's already open (the user may be
    # driving a picker). The toolbar closes on final_answer via end_turn.
    entry = _auto_toolbar.get(window_id)
    if entry is not None:
        _prompt_sent, _opened, _notified = entry
        _auto_toolbar[window_id] = (_prompt_sent, _opened, False)


def mark_prompt_sent(window_id: str) -> None:
    """Record that a user prompt was forwarded to the agent.

    Called by agent_input.deliver_to_agent. Starts the auto-toolbar clock —
    if the agent produces no output for _HANGING_THRESHOLD_SECS, the toolbar
    is auto-opened.
    """
    _auto_toolbar[window_id] = (time.monotonic(), False, False)


def forget_window(window_id: str) -> None:
    """Drop bookkeeping for a window (called on archive / unbind)."""
    _last_activity.pop(window_id, None)
    _auto_toolbar.pop(window_id, None)
    # Marker monitor cleanup.
    try:
        from ..shell_marker import clear_window as _clear_shell
    except ImportError:
        return
    _clear_shell(window_id)


# Slack errors that mean the channel is gone for good — stop trying to post.
CHANNEL_GONE_ERRORS = frozenset({"channel_not_found", "is_archived"})


def is_channel_gone(error: str | None) -> bool:
    """True when a Slack error code means the channel no longer exists."""
    return bool(error) and error in CHANNEL_GONE_ERRORS


def prune_channel(channel_id: str, window_id: str | None = None) -> None:
    """Drop a channel binding + its window state when the channel is gone.

    Called when a post fails with ``channel_not_found`` / ``is_archived`` —
    the Slack channel was deleted or archived out from under us, so there's
    nothing to recover. Removing the binding stops the poll loop from
    retrying (and spamming the log) every tick. Idempotent.
    """
    # Lazy: avoid pulling session/router at module import for this cold path.
    from ...thread_router import thread_router
    from ...window_state_store import window_store

    wid = window_id or thread_router.get_window_for_channel(channel_id)
    unbound = thread_router.unbind_channel(channel_id)
    if wid is None:
        wid = unbound
    if wid:
        with contextlib.suppress(KeyError):
            window_store.remove_window(wid)
        forget_window(wid)
    # Drop any open tool-call thread state for the channel.
    try:
        from ..messaging_pipeline.turn_threads import clear_channel
    except ImportError:
        pass
    else:
        clear_channel(channel_id)
    thread_router.clear_chat_threads(channel_id)
    thread_router.clear_channel_grants(channel_id)
    from ..purge import forget_channel as _purge_forget
    _purge_forget(channel_id)
    logger.info("Pruned gone channel %s (window %s) — binding removed", channel_id, wid)


# How often the poll loop runs the autopurge sweep (delete output past each
# channel's window). Coarse — autopurge granularity is hours, not seconds.
_AUTOPURGE_SWEEP_INTERVAL = 300.0


def start_status_polling(client: SlackClient) -> asyncio.Task[None]:
    """Spawn the background polling task. Returns the asyncio task handle."""
    global _poll_task
    if _poll_task is not None and not _poll_task.done():
        return _poll_task
    _poll_task = asyncio.create_task(_poll_loop(client), name="ccslack-status-poll")
    _poll_task.add_done_callback(task_done_callback)
    logger.info("Status polling started (interval=%.2fs)", config.status_poll_interval)
    return _poll_task


async def stop_status_polling() -> None:
    """Cancel the polling task. Idempotent."""
    global _poll_task
    if _poll_task is None:
        return
    _poll_task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await _poll_task
    _poll_task = None
    logger.info("Status polling stopped")


async def _poll_loop(client: SlackClient) -> None:
    """The polling body — never returns until cancelled."""
    # Lazy: handler modules pull session_manager + slack helpers; defer to
    # keep the polling import path lean for tests.
    from ..status import update_status

    interval = max(0.5, config.status_poll_interval)
    last_sweep = 0.0
    while True:
        try:
            await _tick(client, update_status)
            now = time.monotonic()
            if now - last_sweep >= _AUTOPURGE_SWEEP_INTERVAL:
                last_sweep = now
                from ..purge import sweep

                await sweep(client)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 — never let one bad tick kill the loop
            logger.exception("status poll tick failed")
        await asyncio.sleep(interval)


async def _tick(client: SlackClient, update_status) -> None:  # noqa: ANN001
    """One pass over all bound channels."""
    bindings = list(thread_router.channel_bindings.items())
    if not bindings:
        return
    now = time.monotonic()
    for channel_id, window_id in bindings:
        state = window_store.window_states.get(window_id)
        if state is None:
            continue

        live_window = await tmux_manager.find_window_by_id(window_id)
        if live_window is None:
            await _handle_dead(client, channel_id, window_id, state, update_status)
            continue

        # Active→idle decay. Only flip if nothing else has touched the status
        # state in the meantime (done / dead are terminal until reset).
        if state.status_state == "active":
            last = _last_activity.get(window_id)
            if last is not None and (now - last) > IDLE_DECAY_SECONDS:
                await update_status(client, channel_id, window_id, "idle")

        # Auto-toolbar: if the agent has been quiet for >2 min after a prompt,
        # auto-open the toolbar (likely waiting for human input/approval).
        # At >10 min, @channel once. Closes on final_answer via end_turn.
        await _check_auto_toolbar(client, channel_id, window_id, now)

        # Marker-driven shell monitor: for shell-provider windows, passively
        # poll the pane and relay output as it streams. Falls back silently
        # when the marker isn't present (handlers/shell_capture.py picks up
        # the per-send pane-diff path).
        # Lazy: shell_marker pulls slack_sender; defer to keep the polling
        # import graph light.
        from ..shell_marker import check_passive_shell_output, is_shell_window

        if is_shell_window(window_id):
            await check_passive_shell_output(
                client, channel_id=channel_id, window_id=window_id
            )


async def _check_auto_toolbar(
    client: SlackClient,
    channel_id: str,
    window_id: str,
    now: float,
) -> None:
    """Auto-open toolbar on hang, @channel on extended hang.

    If the agent hasn't produced output for >2 min after a prompt, auto-open
    the toolbar so the user can drive a picker / approve. At >10 min, post
    @channel once. The toolbar auto-closes when a final answer arrives
    (end_turn is called from message_routing on phase=final_answer).
    """
    entry = _auto_toolbar.get(window_id)
    if entry is None:
        return
    prompt_sent_at, toolbar_opened, hang_notified = entry
    elapsed = now - prompt_sent_at

    # Reset if the agent has produced recent output (it's working, not hanging).
    last_activity = _last_activity.get(window_id)
    if last_activity is not None and (now - last_activity) < IDLE_DECAY_SECONDS:
        # Agent is active — reset the hang timer but keep toolbar state.
        _auto_toolbar[window_id] = (now, toolbar_opened, False)
        return

    if elapsed < _HANGING_THRESHOLD_SECS:
        return  # not hanging yet

    # Auto-open the toolbar (once).
    if not toolbar_opened:
        # Lazy: toolbar pulls slack_sender; keep off the hot import path.
        from ..toolbar import open_toolbar

        try:
            await open_toolbar(client, channel_id, window_id)
        except Exception:  # noqa: BLE001 — best-effort, never crash the poll loop
            logger.exception("auto-toolbar open failed for %s", window_id)
        toolbar_opened = True
        _auto_toolbar[window_id] = (prompt_sent_at, toolbar_opened, hang_notified)
        logger.info(
            "Auto-opened toolbar for window %s (idle %.0fs after prompt)",
            window_id,
            elapsed,
        )

    # Extended hang: @channel once.
    if elapsed >= _EXTENDED_HANG_SECS and not hang_notified:
        from ...slack_client import BoltSlackClient

        bolt = BoltSlackClient(client)
        with contextlib.suppress(Exception):
            await bolt.chat_postMessage(
                channel=channel_id,
                text=(
                    f":bell: <!channel> Agent has been waiting for input "
                    f"for {int(elapsed / 60)} min in <#{channel_id}>. "
                    "Use the toolbar to approve or respond."
                ),
            )
        hang_notified = True
        _auto_toolbar[window_id] = (prompt_sent_at, toolbar_opened, hang_notified)
        logger.info(
            "Sent @channel hang notification for window %s (%.0fs)",
            window_id,
            elapsed,
        )


async def _handle_dead(
    client: SlackClient,
    channel_id: str,
    window_id: str,
    state,  # noqa: ANN001
    update_status,  # noqa: ANN001
) -> None:
    """Window vanished from tmux — flip status + post recovery banner once."""
    if state.status_state == "dead":
        return
    await update_status(client, channel_id, window_id, "dead")
    # Lazy: recovery module exists once task 14 lands.
    try:
        from ..recovery import post_recovery_banner
    except ImportError:
        logger.debug("recovery banner not yet wired; skipping")
        return
    await post_recovery_banner(client, channel_id, window_id)


__all__ = [
    "CHANNEL_GONE_ERRORS",
    "IDLE_DECAY_SECONDS",
    "forget_window",
    "is_channel_gone",
    "mark_active",
    "mark_prompt_sent",
    "prune_channel",
    "start_status_polling",
    "stop_status_polling",
]
