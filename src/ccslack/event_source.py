"""Pluggable inbound Slack event sources + the dispatch seam.

ccslack normally receives events over a live Socket Mode connection. To let a
*worker* process (in a multi-host router/worker deployment) instead receive
events forwarded from a router, the inbound source is pluggable while the rest
of the app — handlers, tmux, posting — stays identical.

  * ``dispatch_payload(app, payload)`` — feed a raw Slack event payload into the
    Bolt app, exactly as the Socket Mode adapter does per envelope. This is the
    seam a worker uses for forwarded events.
  * ``EventSource`` — start/stop an inbound source.
  * ``SocketModeSource`` — the default: a live Socket Mode connection.
"""

from __future__ import annotations

import abc
import structlog
from typing import TYPE_CHECKING

from slack_bolt.request.async_request import AsyncBoltRequest

from .config import config

if TYPE_CHECKING:
    from slack_bolt.async_app import AsyncApp

logger = structlog.get_logger()


async def dispatch_payload(app: AsyncApp, payload: dict) -> None:
    """Dispatch a raw Slack event payload into the Bolt app (no socket).

    Mirrors ``slack_bolt``'s Socket Mode adapter, which builds an
    ``AsyncBoltRequest(mode="socket_mode", body=payload)`` and calls
    ``app.async_dispatch``. The Bolt response is intentionally ignored: in a
    worker, the router has already acked Slack; standalone never reaches here.

    Like the standalone path (``process_before_response=False``), the listener
    runs as a background task — this returns once the work is *scheduled*, not
    once it completes — so the caller can immediately handle the next event.
    """
    request = AsyncBoltRequest(mode="socket_mode", body=payload)
    await app.async_dispatch(request)


class EventSource(abc.ABC):
    """An inbound source of Slack events for the Bolt app."""

    @abc.abstractmethod
    async def start(self) -> None:
        """Begin delivering events into the app."""

    @abc.abstractmethod
    async def stop(self) -> None:
        """Stop delivering events and release resources."""


class SocketModeSource(EventSource):
    """Inbound events from a live Slack Socket Mode connection (the default).

    This is the standalone path: one process, one Socket Mode connection. In a
    router/worker deployment only the router runs this; workers use a forwarding
    source that calls :func:`dispatch_payload` instead.
    """

    def __init__(self, app: AsyncApp) -> None:
        self._app = app
        # Lazy type: the aiohttp adapter is imported at start() to keep the
        # import graph lean for processes that never open a socket.
        self._handler: object | None = None

    async def start(self) -> None:
        # Lazy: pulls the aiohttp websocket stack only when actually connecting.
        from slack_bolt.adapter.socket_mode.aiohttp import AsyncSocketModeHandler

        handler = AsyncSocketModeHandler(self._app, config.slack_app_token)
        self._handler = handler
        await handler.connect_async()
        logger.info("Socket Mode connected; ccslack ready")

    async def stop(self) -> None:
        if self._handler is not None:
            try:
                await self._handler.close_async()
            except Exception:  # noqa: BLE001 — best-effort shutdown
                logger.exception("Error closing Socket Mode handler")
            self._handler = None


__all__ = ["EventSource", "SocketModeSource", "dispatch_payload"]
