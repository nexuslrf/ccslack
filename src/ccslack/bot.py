"""Slack Bolt app factory + lifecycle delegates.

Builds the ``AsyncApp`` with the bot token, registers handlers from
``handlers.registry``, and exposes a ``create_app`` factory plus ``start`` /
``stop`` coroutines that drive an inbound :class:`~ccslack.event_source.EventSource`.

Responsibilities kept here:
  - ``create_app`` — Bolt ``AsyncApp`` factory.
  - ``start_event_source`` / ``stop_event_source`` — bootstrap + source lifecycle.
  - ``start_socket_mode`` / ``stop_socket_mode`` — back-compat aliases that use
    the default :class:`SocketModeSource` (the standalone path).
  - Global error handler.

Actual handler bodies live under ``handlers/`` and are registered by
``handlers.registry.register_all``.
"""

from __future__ import annotations

import structlog
from slack_bolt.async_app import AsyncApp

from . import bootstrap
from .config import config
from .event_source import EventSource, SocketModeSource
from .handlers.registry import register_all

logger = structlog.get_logger()


async def _global_error_handler(
    error: Exception, body: dict, logger_: structlog.stdlib.BoundLogger | None = None
) -> None:
    """Top-level error funnel for unhandled handler errors.

    Returns ``None`` on purpose — Bolt asserts that a non-None
    ``BoltResponse`` from this hook must be paired with a non-None response on
    the originating request, which isn't the case for listener exceptions
    raised before ``ack()``. Logging + swallowing is the safe default.
    """
    log = logger_ or logger
    log.error("Unhandled handler error", exc_info=error, body_type=body.get("type"))
    return None


def create_app() -> AsyncApp:
    """Build the Bolt ``AsyncApp`` for ccslack."""
    app = AsyncApp(
        token=config.slack_bot_token,
        # Process events sequentially in the order Slack delivers them. This is
        # important for message ordering inside a channel; concurrency comes
        # from the per-channel send queue (handlers/messaging_pipeline).
        process_before_response=False,
        raise_error_for_unhandled_request=False,
    )
    app.error(_global_error_handler)
    register_all(app)
    return app


_event_source: EventSource | None = None


async def start_event_source(app: AsyncApp, source: EventSource) -> EventSource:
    """Run post-init bootstrap, then start the given inbound event source.

    Runs ``bootstrap.bootstrap_application`` for post-init wiring (session
    monitor, status polling) before the source begins delivering events. The
    standalone path passes a :class:`SocketModeSource`; a worker passes a
    forwarding source.
    """
    global _event_source
    await bootstrap.bootstrap_application(app)
    _event_source = source
    await source.start()
    return source


async def stop_event_source() -> None:
    """Stop the active event source and tear down runtime state."""
    global _event_source
    if _event_source is not None:
        await _event_source.stop()
        _event_source = None
    await bootstrap.shutdown_runtime()


async def start_socket_mode(app: AsyncApp) -> EventSource:
    """Standalone entry point: bootstrap + a live Socket Mode connection."""
    return await start_event_source(app, SocketModeSource(app))


async def stop_socket_mode() -> None:
    """Disconnect the event source and tear down runtime state."""
    await stop_event_source()


__all__ = [
    "create_app",
    "start_event_source",
    "start_socket_mode",
    "stop_event_source",
    "stop_socket_mode",
]
