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

import re
import structlog
from collections.abc import Awaitable, Callable, Iterator
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

# `--host <name>` inside a slash command's text (e.g. `/ccslack new <dir> --host gpu1`).
_HOST_DIRECTIVE_RE = re.compile(r"(?:^|\s)--host[=\s]+(\S+)")


def host_directive(payload: dict[str, Any]) -> str | None:
    """Extract a ``--host <name>`` selector from a slash command payload, if any."""
    if not isinstance(payload, dict) or "command" not in payload:
        return None
    match = _HOST_DIRECTIVE_RE.search(payload.get("text") or "")
    return match.group(1) if match else None


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
        self.connected_hosts: set[str] = set()
        self._forwarder: Forwarder | None = None

    def set_forwarder(self, forwarder: Forwarder) -> None:
        self._forwarder = forwarder

    def channel_host_items(self) -> Iterator[tuple[str, str]]:
        """Iterate ``(channel_id, host)`` for every registered channel."""
        yield from self._channel_host.items()

    # --- registry mutations (driven by worker link messages) ---------------

    def set_host_channels(self, host: str, channels: list[str]) -> None:
        """Replace *host*'s entire channel set (a worker's ``hello`` snapshot).

        Also marks the host connected — a worker with zero sessions still counts
        as an available host for ``--host`` routing.
        """
        self.connected_hosts.add(host)
        self._channel_host = {
            ch: h for ch, h in self._channel_host.items() if h != host
        }
        for channel in channels:
            if channel:
                self._channel_host[channel] = host

    def bind(self, host: str, channel: str) -> None:
        self.connected_hosts.add(host)
        if channel:
            self._channel_host[channel] = host

    def unbind(self, host: str, channel: str) -> None:
        if self._channel_host.get(channel) == host:
            self._channel_host.pop(channel, None)

    def drop_host(self, host: str) -> None:
        """Forget all of a host's channels + mark it disconnected."""
        self.connected_hosts.discard(host)
        self._channel_host = {
            ch: h for ch, h in self._channel_host.items() if h != host
        }

    # --- routing ------------------------------------------------------------

    def host_for_channel(self, channel: str) -> str | None:
        return self._channel_host.get(channel)

    def target_host(self, payload: dict[str, Any]) -> str | None:
        """The remote host that should handle this event, or None for local.

        A ``--host <name>`` directive on a slash command forwards to that worker
        when it's a *connected remote* host. An unknown/disconnected/local host
        falls through to local dispatch — where ``_handle_new`` reports it if
        the host isn't this one.
        """
        directive = host_directive(payload)
        if directive is not None:
            if directive != self.local_host and directive in self.connected_hosts:
                return directive
            return None
        channel = channel_of(payload)
        if not channel:
            return None
        host = self._channel_host.get(channel)
        if host is None or host == self.local_host:
            return None
        return host

    async def forward(self, host: str, payload: dict[str, Any]) -> bool:
        """Forward a payload to *host*'s worker. Returns True on success."""
        if self._forwarder is None:
            logger.warning("router: no forwarder; dropping event for host %s", host)
            return False
        ok = await self._forwarder(host, payload)
        if not ok:
            logger.warning("router: forward to host %s failed", host)
        return ok


async def route_and_dispatch(
    client: AsyncBaseSocketModeClient,
    req: SocketModeRequest,
    app: AsyncApp,
    router: Router,
) -> None:
    """Dispatch a Socket Mode request locally, or forward it to the owning host.

    * **Local** target: ack immediately (ccslack replies via ephemeral/channel
      posts, not the synchronous slash response, so an empty ack loses nothing),
      then dispatch into the local Bolt app (listeners run backgrounded).
    * **Remote** target: forward *first*, ack only on success. If the worker
      link is momentarily down the forward fails and we do NOT ack, so Slack
      redelivers (to this same router connection) a few times — covering a brief
      worker blip instead of dropping the event.
    """
    host = router.target_host(req.payload)
    if host is None:
        await client.send_socket_mode_response(
            SocketModeResponse(envelope_id=req.envelope_id)
        )
        await run_async_bolt_app(app, req)
        return
    if await router.forward(host, req.payload):
        await client.send_socket_mode_response(
            SocketModeResponse(envelope_id=req.envelope_id)
        )


class _RoutingHandler(AsyncSocketModeHandler):
    """Socket Mode handler that acks, then dispatches locally or forwards."""

    def __init__(self, app: AsyncApp, router: Router, app_token: str) -> None:
        super().__init__(app, app_token, proxy=config.proxy or None)
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
