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

_poll_task: asyncio.Task[None] | None = None

# Track last-seen activity per window_id (monotonic seconds). Updated when
# message_routing posts content for a window.
_last_activity: dict[str, float] = {}


def mark_active(window_id: str) -> None:
    """Bookkeeping: called by message_routing when fresh content arrives."""
    _last_activity[window_id] = time.monotonic()


def forget_window(window_id: str) -> None:
    """Drop bookkeeping for a window (called on archive / unbind)."""
    _last_activity.pop(window_id, None)
    # Lazy: prompt_probe state is best-effort; absent module is fine.
    try:
        from .prompt_probe import clear_window as _clear_probe
    except ImportError:
        pass
    else:
        _clear_probe(window_id)
    # Drop the dismiss cooldown too — channel/window is going away.
    try:
        from ..interactive import session_for_window as _session_for_window
        from .prompt_probe import clear_dismiss as _clear_dismiss
    except ImportError:
        pass
    else:
        # No direct channel→window reverse here; clear by iterating dismiss map
        # would need the channel id. Best-effort: skip if we can't get it.
        session = _session_for_window(window_id)
        if session is not None:
            _clear_dismiss(session.channel_id)
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
    logger.info(
        "Pruned gone channel %s (window %s) — binding removed", channel_id, wid
    )


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
    while True:
        try:
            await _tick(client, update_status)
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

        # Probe for interactive prompts every tick — the prompt_probe module
        # gates internally on hash + interactive-mode + hook-driven providers,
        # so calling it unconditionally won't spam. We can't wait for
        # active→idle decay (5 s) because Codex's exec-approval prompt sits on
        # top of an "active" pane the instant streaming pauses.
        from .prompt_probe import maybe_post_prompt

        await maybe_post_prompt(client, channel_id, window_id)

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
    # Close any live interactive picker bound to the now-dead window so users
    # don't see a frozen picker that no longer accepts keys.
    # Lazy: import here to avoid bootstrap cycle.
    from ..interactive import exit_for_window

    await exit_for_window(client, window_id, reason="window died")

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
    "prune_channel",
    "start_status_polling",
    "stop_status_polling",
]
