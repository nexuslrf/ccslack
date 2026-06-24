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
import asyncio
import contextlib
import structlog
from typing import TYPE_CHECKING, Any

from slack_bolt.request.async_request import AsyncBoltRequest

from . import link
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
        if not config.slack_app_token:
            raise ValueError(
                "SLACK_APP_TOKEN is required for Socket Mode (standalone / router). "
                "A --worker doesn't need it."
            )
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


class RouterLinkSource(EventSource):
    """Worker inbound source: serve the router link and dispatch forwarded events.

    Runs a localhost TCP server that the router connects to (through an SSH
    tunnel). On the router's connection it sends a ``hello`` (this host + the
    channels it owns), streams forwarded Slack events into the Bolt app via
    :func:`dispatch_payload`, pushes live bind/unbind ownership changes back,
    and answers pings. Outbound posting to Slack is unchanged (direct, bot
    token) — only the inbound source differs from standalone.

    One router connection is handled at a time; a reconnect replaces the prior
    link and re-sends the hello snapshot.
    """

    def __init__(
        self,
        app: AsyncApp,
        *,
        host: str,
        bind_host: str = "127.0.0.1",
        port: int = 0,
    ) -> None:
        self._app = app
        self._host = host
        self._bind_host = bind_host
        self._port = port
        self._server: asyncio.AbstractServer | None = None
        self._outbound: asyncio.Queue[dict[str, Any]] | None = None

    @property
    def port(self) -> int:
        """The actual bound port (resolves an ephemeral ``port=0``)."""
        if self._server is not None and self._server.sockets:
            return self._server.sockets[0].getsockname()[1]
        return self._port

    async def start(self) -> None:
        # Lazy: thread_router is wired by SessionManager; import at call site.
        from .thread_router import thread_router

        thread_router.add_binding_listener(self._on_binding_change)
        self._server = await asyncio.start_server(
            self._handle_connection, self._bind_host, self._port
        )
        logger.info(
            "Worker link listening on %s:%d (host=%s)",
            self._bind_host,
            self.port,
            self._host,
        )

    async def stop(self) -> None:
        from .thread_router import thread_router

        thread_router.remove_binding_listener(self._on_binding_change)
        if self._server is not None:
            self._server.close()
            with contextlib.suppress(Exception):
                await self._server.wait_closed()
            self._server = None
        self._outbound = None

    def _on_binding_change(self, action: str, channel_id: str) -> None:
        # Sync, fired on the loop during event handling. Only push while the
        # router is connected; a reconnect re-syncs via the hello snapshot.
        if self._outbound is not None:
            self._outbound.put_nowait({"t": action, "channel": channel_id})

    async def _handle_connection(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        from .thread_router import thread_router

        outbound: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self._outbound = outbound
        await link.write_msg(
            writer, link.hello(self._host, list(thread_router.channel_bindings))
        )
        drain = asyncio.create_task(self._drain_outbound(writer, outbound))
        peer = writer.get_extra_info("peername")
        logger.info("Router connected to worker link: %s", peer)
        try:
            while True:
                msg = await link.read_msg(reader)
                if msg is None:
                    break
                tag = msg.get("t")
                if tag == link.EVENT:
                    payload = msg.get("payload")
                    if isinstance(payload, dict):
                        await dispatch_payload(self._app, payload)
                elif tag == link.PING:
                    outbound.put_nowait({"t": link.PONG})
        finally:
            drain.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await drain
            with contextlib.suppress(Exception):
                writer.close()
            if self._outbound is outbound:
                self._outbound = None
            logger.info("Router disconnected from worker link: %s", peer)

    @staticmethod
    async def _drain_outbound(
        writer: asyncio.StreamWriter, outbound: asyncio.Queue[dict[str, Any]]
    ) -> None:
        while True:
            msg = await outbound.get()
            await link.write_msg(writer, msg)


__all__ = [
    "EventSource",
    "RouterLinkSource",
    "SocketModeSource",
    "dispatch_payload",
]
