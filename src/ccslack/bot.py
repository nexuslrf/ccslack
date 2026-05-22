"""Slack Bolt app factory + lifecycle delegates.

Builds the ``AsyncApp`` with the bot token, registers handlers from
``handlers.registry``, and exposes a ``create_app`` factory plus ``start`` /
``stop`` coroutines that drive Socket Mode.

Responsibilities kept here:
  - ``create_app`` — Bolt ``AsyncApp`` factory.
  - ``start_socket_mode`` / ``stop_socket_mode`` — Socket Mode lifecycle.
  - Global error handler.

Actual handler bodies live under ``handlers/`` and are registered by
``handlers.registry.register_all``.
"""

from __future__ import annotations

import structlog
from slack_bolt.async_app import AsyncApp
from slack_bolt.adapter.socket_mode.aiohttp import AsyncSocketModeHandler

from . import bootstrap
from .config import config
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


_socket_handler: AsyncSocketModeHandler | None = None


async def start_socket_mode(app: AsyncApp) -> AsyncSocketModeHandler:
    """Connect to Slack via Socket Mode and start handling events.

    Runs ``bootstrap.bootstrap_application`` for post-init wiring (session
    monitor, status polling) before opening the socket.
    """
    global _socket_handler
    await bootstrap.bootstrap_application(app)
    handler = AsyncSocketModeHandler(app, config.slack_app_token)
    _socket_handler = handler
    await handler.connect_async()
    logger.info("Socket Mode connected; ccslack ready")
    return handler


async def stop_socket_mode() -> None:
    """Disconnect Socket Mode and tear down runtime state."""
    global _socket_handler
    if _socket_handler is not None:
        try:
            await _socket_handler.close_async()
        except Exception:  # noqa: BLE001 — best-effort shutdown
            logger.exception("Error closing Socket Mode handler")
        _socket_handler = None
    await bootstrap.shutdown_runtime()


__all__ = [
    "create_app",
    "start_socket_mode",
    "stop_socket_mode",
]
