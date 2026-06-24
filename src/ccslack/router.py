"""Router role: the single Socket Mode intake that routes events to the owning host.

In a multi-host fleet exactly one process runs as the router. It holds the only
Socket Mode connection and keeps a ``channel_id -> host`` registry (fed by worker
``hello`` / ``bind`` / ``unbind`` over the link). For each inbound Slack event it
**acks Slack immediately** (the 3s Socket Mode window), then either dispatches
locally — the router's own host, which also runs sessions — or forwards the raw
payload to the owning worker.

Phase 2 scope: registry + routing decision + local dispatch. There are no remote
workers connected yet, so every event routes local and a single-host router
behaves exactly like standalone. Remote tunnels + forwarding arrive in phase 3.
"""

from __future__ import annotations

import structlog
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

from slack_bolt.adapter.socket_mode.aiohttp import AsyncSocketModeHandler
from slack_bolt.adapter.socket_mode.async_internals import run_async_bolt_app
from slack_sdk.socket_mode.response import SocketModeResponse

from .config import config
from .event_source import EventSource

if TYPE_CHECKING:
    from slack_bolt.async_app import AsyncApp
    from slack_sdk.socket_mode.async_client import AsyncBaseSocketModeClient
    from slack_sdk.socket_mode.request import SocketModeRequest

logger = structlog.get_logger()

# Forwards a raw Slack payload to a remote host's worker. Returns True on success.
Forwarder = Callable[[str, dict[str, Any]], Awaitable[bool]]


def channel_of(payload: dict[str, Any]) -> str | None:
    """Extract the channel_id an event pertains to, across Slack payload shapes.

    Slash commands carry ``channel_id``; interactive payloads (block actions)
    carry ``channel.id``; Events API payloads carry ``event.channel``. Returns
    ``None`` when there's no channel (e.g. a view submission), which routes local.
    """
    if not isinstance(payload, dict):
        return None
    if payload.get("channel_id"):
        return payload["channel_id"]
    channel = payload.get("channel")
    if isinstance(channel, dict):
        return channel.get("id") or None
    if isinstance(channel, str):
        return channel or None
    event = payload.get("event")
    if isinstance(event, dict):
        ev_channel = event.get("channel")
        if isinstance(ev_channel, str):
            return ev_channel or None
        if isinstance(ev_channel, dict):
            return ev_channel.get("id") or None
        return event.get("channel_id") or None
    return None


class Router:
    """The ``channel_id -> host`` registry + routing decision.

    Only *remote* channels are tracked; anything unknown (or owned by the
    router's own host) routes local. Workers feed the registry via the link.
    """

    def __init__(self, local_host: str) -> None:
        self.local_host = local_host
        self._channel_host: dict[str, str] = {}
        self._forwarder: Forwarder | None = None

    def set_forwarder(self, forwarder: Forwarder) -> None:
        self._forwarder = forwarder

    # --- registry mutations (driven by worker link messages) ---------------

    def set_host_channels(self, host: str, channels: list[str]) -> None:
        """Replace *host*'s entire channel set (a worker's ``hello`` snapshot)."""
        self._channel_host = {
            ch: h for ch, h in self._channel_host.items() if h != host
        }
        for channel in channels:
            if channel:
                self._channel_host[channel] = host

    def bind(self, host: str, channel: str) -> None:
        if channel:
            self._channel_host[channel] = host

    def unbind(self, host: str, channel: str) -> None:
        if self._channel_host.get(channel) == host:
            self._channel_host.pop(channel, None)

    def drop_host(self, host: str) -> None:
        """Forget all of a host's channels (on disconnect)."""
        self._channel_host = {
            ch: h for ch, h in self._channel_host.items() if h != host
        }

    # --- routing ------------------------------------------------------------

    def host_for_channel(self, channel: str) -> str | None:
        return self._channel_host.get(channel)

    def target_host(self, payload: dict[str, Any]) -> str | None:
        """The remote host that should handle this event, or None for local."""
        channel = channel_of(payload)
        if not channel:
            return None
        host = self._channel_host.get(channel)
        if host is None or host == self.local_host:
            return None
        return host

    async def forward(self, host: str, payload: dict[str, Any]) -> None:
        if self._forwarder is None:
            logger.warning("router: no forwarder; dropping event for host %s", host)
            return
        if not await self._forwarder(host, payload):
            logger.warning("router: forward to host %s failed", host)


async def route_and_dispatch(
    client: AsyncBaseSocketModeClient,
    req: SocketModeRequest,
    app: AsyncApp,
    router: Router,
) -> None:
    """Ack the Socket Mode request, then dispatch locally or forward to a host.

    Ack first: Socket Mode requires a response within ~3s, and ccslack replies
    via ephemeral/channel posts rather than the synchronous slash response, so
    an empty ack loses nothing. A local target dispatches into the Bolt app
    (response ignored — already acked); a remote target is forwarded.
    """
    await client.send_socket_mode_response(
        SocketModeResponse(envelope_id=req.envelope_id)
    )
    host = router.target_host(req.payload)
    if host is None:
        await run_async_bolt_app(app, req)
        return
    await router.forward(host, req.payload)


class _RoutingHandler(AsyncSocketModeHandler):
    """Socket Mode handler that acks, then dispatches locally or forwards."""

    def __init__(self, app: AsyncApp, router: Router, app_token: str) -> None:
        super().__init__(app, app_token)
        self._router = router

    async def handle(  # type: ignore[override]
        self, client: AsyncBaseSocketModeClient, req: SocketModeRequest
    ) -> None:
        await route_and_dispatch(client, req, self.app, self._router)


class RouterSource(EventSource):
    """Inbound source for the router role: Socket Mode + per-event routing."""

    def __init__(self, app: AsyncApp, router: Router) -> None:
        self._app = app
        self._router = router
        self._handler: _RoutingHandler | None = None

    async def start(self) -> None:
        if not config.slack_app_token:
            raise ValueError("SLACK_APP_TOKEN is required for the router (Socket Mode).")
        self._handler = _RoutingHandler(self._app, self._router, config.slack_app_token)
        await self._handler.connect_async()
        logger.info("Router Socket Mode connected (host=%s)", self._router.local_host)

    async def stop(self) -> None:
        if self._handler is not None:
            try:
                await self._handler.close_async()
            except Exception:  # noqa: BLE001 — best-effort shutdown
                logger.exception("Error closing router Socket Mode handler")
            self._handler = None


__all__ = ["Router", "RouterSource", "channel_of", "route_and_dispatch"]
