"""Application bootstrap — wires post-init and post-shutdown lifecycle.

``bot.start_socket_mode`` calls ``bootstrap_application`` before opening the
websocket, and ``bot.stop_socket_mode`` calls ``shutdown_runtime`` afterwards.

Ordering invariant: runtime callback wiring must happen before
``start_session_monitor`` because the monitor dispatches events that look up
registered callbacks and fail loud if unwired.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

import structlog

from .session import session_manager
from .session_monitor import (
    NewMessage,
    SessionMonitor,
    clear_active_monitor,
    set_active_monitor,
)
from .slack_client import BoltSlackClient
from .utils import task_done_callback

if TYPE_CHECKING:
    from slack_bolt.async_app import AsyncApp

    from .providers.base import HookEvent

logger = structlog.get_logger()

session_monitor: SessionMonitor | None = None
_status_poll_task: asyncio.Task[None] | None = None
_callbacks_wired = False


def install_global_exception_handler() -> None:
    """Install an asyncio last-resort handler that logs unhandled exceptions."""
    asyncio.get_running_loop().set_exception_handler(_global_exception_handler)


def _global_exception_handler(
    _loop: asyncio.AbstractEventLoop, ctx: dict[str, object]
) -> None:
    exc = ctx.get("exception")
    msg = ctx.get("message", "Unhandled exception in event loop")
    if isinstance(exc, BaseException):
        logger.error(
            "asyncio exception handler: %s",
            msg,
            exc_info=(type(exc), exc, exc.__traceback__),
        )
    else:
        logger.error("asyncio exception handler: %s", msg)


def wire_runtime_callbacks() -> None:
    """Wire module-level callbacks. Idempotent."""
    global _callbacks_wired
    if _callbacks_wired:
        return
    _callbacks_wired = True


async def start_session_monitor(app: AsyncApp) -> SessionMonitor:
    """Build the SessionMonitor and start polling.

    Callbacks (message routing, hook dispatch, new-window adoption) are wired
    once handler implementations land. The skeleton bot starts the monitor
    without callbacks so transcripts are tracked but not forwarded.
    """
    global session_monitor

    monitor = SessionMonitor()
    set_active_monitor(monitor)

    client = BoltSlackClient(app.client)

    # Lazy: handler modules pull a lot of state at import time. Deferring keeps
    # the bootstrap import graph small while the walking skeleton lands.
    # Lazy: handlers register optional callbacks; absent ones are silently skipped.
    try:
        from .handlers.messaging_pipeline import message_routing  # noqa: F401
    except ImportError:
        logger.debug("messaging_pipeline not present; skipping message callback")
    else:
        # Lazy: same module just imported above.
        from .handlers.messaging_pipeline.message_routing import (
            handle_new_message,
        )

        async def message_callback(msg: NewMessage) -> None:
            await handle_new_message(msg, client)

        monitor.set_message_callback(message_callback)

    try:
        # Lazy: hook event dispatcher only exists once handlers land.
        from .handlers.hook_events import dispatch_hook_event
    except ImportError:
        logger.debug("hook_events not present; skipping hook callback")
    else:

        async def hook_event_callback(event: HookEvent) -> None:
            await dispatch_hook_event(event, client)

        monitor.set_hook_event_callback(hook_event_callback)

    monitor.start()
    session_monitor = monitor
    logger.info("Session monitor started")
    return monitor


async def bootstrap_application(app: AsyncApp) -> None:
    """Run the full post-init sequence in the prescribed order."""
    install_global_exception_handler()
    await session_manager.resolve_stale_ids()
    wire_runtime_callbacks()
    await start_session_monitor(app)
    await _start_status_polling(app)
    await _restore_dead_windows(app)


async def _restore_dead_windows(app: AsyncApp) -> None:
    """Auto-recover sessions whose tmux window died (reboot / tmux restart).

    Gated on ``config.restore_on_start``; a no-op for ``off`` / ``banner``.
    Runs after polling has started so the recovery banner remains the backstop
    for anything auto-recovery skips.
    """
    try:
        from .handlers.recovery import restore_dead_windows_on_start
    except ImportError:
        logger.debug("recovery module not present; skipping startup restore")
        return
    client = BoltSlackClient(app.client)
    try:
        await restore_dead_windows_on_start(client)
    except Exception:  # noqa: BLE001 — never let restore break startup
        logger.exception("startup restore failed")


async def _start_status_polling(app: AsyncApp) -> None:
    """Start the polling coordinator if the module is wired."""
    # Lazy: polling subpackage is optional during early skeleton development.
    try:
        from .handlers.polling import start_status_polling
    except ImportError:
        logger.debug("polling subpackage not present; skipping status poll")
        return
    client = BoltSlackClient(app.client)
    start_status_polling(client)


async def shutdown_runtime() -> None:
    """Run the post-shutdown teardown sequence."""
    global session_monitor

    # Lazy: polling subpackage may not be present in early dev.
    try:
        from .handlers.polling import stop_status_polling
    except ImportError:
        pass
    else:
        await stop_status_polling()

    if session_monitor is not None:
        session_monitor.stop()
        logger.info("Session monitor stopped")
        session_monitor = None
    clear_active_monitor()

    session_manager.flush_state()


def reset_for_testing() -> None:
    """Clear bootstrap module state. Tests use this between runs."""
    global _callbacks_wired, session_monitor, _status_poll_task
    _callbacks_wired = False
    session_monitor = None
    _status_poll_task = None
    clear_active_monitor()


__all__ = [
    "bootstrap_application",
    "reset_for_testing",
    "session_monitor",
    "shutdown_runtime",
    "start_session_monitor",
    "wire_runtime_callbacks",
]


# Reference task_done_callback so import-time-only consumers don't strip it.
_ = task_done_callback
