"""Router-side fleet: SSH tunnels + link clients to each worker.

For every configured worker the router keeps:

  * an :class:`SshTunnel` — a persistent ``ssh -N -L`` subprocess that forwards a
    router-local port to the worker's link server (worker behind any network as
    long as bidirectional SSH works), restarted with backoff if it drops; and
  * a :class:`WorkerLink` — the link-protocol *client* that connects to that
    local port, consumes ``hello`` / ``bind`` / ``unbind`` to update the
    :class:`~ccslack.router.Router` registry, sends ``event`` frames when the
    router forwards, pings for liveness, and drops the host's channels on
    disconnect (reconnecting with backoff).

:class:`RouterFleet` owns both per worker and wires ``Router.set_forwarder`` so
``Router.forward(host, payload)`` reaches the right worker's link.

SSH is abstracted behind the ``connect`` callable so the link client is testable
over a plain loopback connection (the transport is irrelevant to the protocol).
"""

from __future__ import annotations

import asyncio
import contextlib
import socket
import structlog
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from . import link

if TYPE_CHECKING:
    from .router import Router

logger = structlog.get_logger()

_BACKOFF_START = 1.0
_BACKOFF_MAX = 30.0
_PING_INTERVAL = 15.0

Connect = Callable[[], Awaitable[tuple[asyncio.StreamReader, asyncio.StreamWriter]]]
# Posts a short fleet status line to the meta channel (host up/down). Best-effort.
Notify = Callable[[str], Awaitable[None]]


@dataclass(frozen=True)
class WorkerSpec:
    """One worker: a host name + the SSH target used to reach it + its link port."""

    host: str
    ssh_target: str
    remote_port: int


def parse_workers(raw: str, default_port: int) -> list[WorkerSpec]:
    """Parse ``CCSLACK_WORKERS`` (``host=ssh_target`` entries, comma-separated).

    ``ssh_target`` is whatever ``ssh <target>`` resolves — a ``~/.ssh/config``
    alias or ``user@host`` — so all SSH auth/proxy config is the user's. Each
    worker's link server is assumed on ``default_port`` (``CCSLACK_LINK_PORT``).
    """
    specs: list[WorkerSpec] = []
    seen: set[str] = set()
    for entry in raw.split(","):
        entry = entry.strip()
        if not entry:
            continue
        host, sep, target = entry.partition("=")
        host, target = host.strip(), target.strip()
        if not sep or not host or not target:
            logger.warning("CCSLACK_WORKERS: ignoring malformed entry %r", entry)
            continue
        if host in seen:
            logger.warning("CCSLACK_WORKERS: duplicate host %r ignored", host)
            continue
        seen.add(host)
        specs.append(WorkerSpec(host=host, ssh_target=target, remote_port=default_port))
    return specs


def _free_local_port() -> int:
    """Pick an unused localhost TCP port for the SSH ``-L`` forward."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class SshTunnel:
    """A supervised ``ssh -N -L`` tunnel to one worker's link server."""

    def __init__(self, ssh_target: str, remote_port: int) -> None:
        self.ssh_target = ssh_target
        self.remote_port = remote_port
        self.local_port = _free_local_port()
        self._proc: asyncio.subprocess.Process | None = None
        self._task: asyncio.Task[None] | None = None
        self._stopping = False

    def build_command(self) -> list[str]:
        return [
            "ssh",
            "-N",
            "-T",
            "-o", "ServerAliveInterval=15",
            "-o", "ServerAliveCountMax=3",
            "-o", "ExitOnForwardFailure=yes",
            "-L", f"{self.local_port}:127.0.0.1:{self.remote_port}",
            self.ssh_target,
        ]

    async def start(self) -> None:
        self._stopping = False
        self._task = asyncio.create_task(self._supervise(), name=f"ssh-{self.ssh_target}")

    async def stop(self) -> None:
        self._stopping = True
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._task
            self._task = None
        await self._terminate()

    async def _terminate(self) -> None:
        if self._proc is not None and self._proc.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                self._proc.terminate()
            with contextlib.suppress(asyncio.TimeoutError, Exception):
                await asyncio.wait_for(self._proc.wait(), timeout=5)
        self._proc = None

    async def _supervise(self) -> None:
        backoff = _BACKOFF_START
        while not self._stopping:
            cmd = self.build_command()
            logger.info("ssh tunnel up: %s (local :%d)", self.ssh_target, self.local_port)
            try:
                # Inherit stdio so an operator at the router console can answer
                # an interactive auth prompt (OTP) when SSH needs it.
                self._proc = await asyncio.create_subprocess_exec(*cmd)
                await self._proc.wait()
            except (OSError, asyncio.CancelledError):
                if self._stopping:
                    return
                logger.exception("ssh tunnel error: %s", self.ssh_target)
            if self._stopping:
                return
            logger.warning(
                "ssh tunnel to %s exited; reconnecting in %.0fs (may need manual auth)",
                self.ssh_target,
                backoff,
            )
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, _BACKOFF_MAX)


class WorkerLink:
    """Router-side link client to one worker (over the tunnel's local port)."""

    def __init__(
        self,
        host: str,
        connect: Connect,
        router: Router,
        notify: Notify | None = None,
    ) -> None:
        self._host = host
        self._connect = connect
        self._router = router
        self._notify = notify
        self._writer: asyncio.StreamWriter | None = None
        self._task: asyncio.Task[None] | None = None
        self._stopping = False
        self._was_connected = False

    async def _announce(self, text: str) -> None:
        if self._notify is None:
            return
        try:
            await self._notify(text)
        except Exception:  # noqa: BLE001 — notification must not break the link
            logger.exception("fleet notify failed")

    async def start(self) -> None:
        self._stopping = False
        self._task = asyncio.create_task(self._run(), name=f"link-{self._host}")

    async def stop(self) -> None:
        self._stopping = True
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._task
            self._task = None
        self._writer = None
        self._router.drop_host(self._host)

    async def forward(self, payload: dict[str, Any]) -> bool:
        writer = self._writer
        if writer is None:
            return False
        try:
            await link.write_msg(writer, link.event(payload))
            return True
        except (ConnectionError, OSError):
            return False

    async def _run(self) -> None:
        backoff = _BACKOFF_START
        while not self._stopping:
            try:
                reader, writer = await self._connect()
            except (OSError, asyncio.CancelledError):
                if self._stopping:
                    return
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, _BACKOFF_MAX)
                continue
            backoff = _BACKOFF_START
            self._writer = writer
            logger.info("worker link connected: %s", self._host)
            await self._announce(f":satellite: host `{self._host}` connected.")
            self._was_connected = True
            ping = asyncio.create_task(self._ping_loop(writer))
            try:
                while True:
                    msg = await link.read_msg(reader)
                    if msg is None:
                        break
                    self._handle(msg)
            finally:
                ping.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await ping
                self._writer = None
                self._router.drop_host(self._host)
                with contextlib.suppress(Exception):
                    writer.close()
                logger.warning("worker link to %s disconnected", self._host)
                if self._was_connected and not self._stopping:
                    self._was_connected = False
                    await self._announce(
                        f":warning: host `{self._host}` disconnected — reconnecting "
                        "(may need manual SSH auth at the router console)."
                    )
            if not self._stopping:
                await asyncio.sleep(backoff)

    def _handle(self, msg: dict[str, Any]) -> None:
        tag = msg.get("t")
        if tag == link.HELLO:
            channels = msg.get("channels") or []
            self._router.set_host_channels(self._host, list(channels))
            logger.info("worker %s owns %d channel(s)", self._host, len(channels))
        elif tag == link.BIND:
            channel = msg.get("channel")
            if channel:
                self._router.bind(self._host, channel)
        elif tag == link.UNBIND:
            channel = msg.get("channel")
            if channel:
                self._router.unbind(self._host, channel)
        # PONG: liveness only — no action needed.

    async def _ping_loop(self, writer: asyncio.StreamWriter) -> None:
        while True:
            await asyncio.sleep(_PING_INTERVAL)
            try:
                await link.write_msg(writer, {"t": link.PING})
            except (ConnectionError, OSError):
                return


class RouterFleet:
    """Owns the SSH tunnels + worker links and wires the router's forwarder."""

    def __init__(
        self,
        router: Router,
        workers: list[WorkerSpec],
        notify: Notify | None = None,
    ) -> None:
        self._router = router
        self._workers = workers
        self._notify = notify
        self._tunnels: dict[str, SshTunnel] = {}
        self._links: dict[str, WorkerLink] = {}

    async def start(self) -> None:
        for spec in self._workers:
            tunnel = SshTunnel(spec.ssh_target, spec.remote_port)
            await tunnel.start()

            async def _connect(
                t: SshTunnel = tunnel,
            ) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
                return await asyncio.open_connection("127.0.0.1", t.local_port)

            worker_link = WorkerLink(
                spec.host, _connect, self._router, notify=self._notify
            )
            await worker_link.start()
            self._tunnels[spec.host] = tunnel
            self._links[spec.host] = worker_link
            logger.info("fleet: worker %s via %s", spec.host, spec.ssh_target)
        self._router.set_forwarder(self._forward)

    async def stop(self) -> None:
        for worker_link in self._links.values():
            await worker_link.stop()
        for tunnel in self._tunnels.values():
            await tunnel.stop()
        self._links.clear()
        self._tunnels.clear()

    async def _forward(self, host: str, payload: dict[str, Any]) -> bool:
        worker_link = self._links.get(host)
        if worker_link is None:
            return False
        return await worker_link.forward(payload)


__all__ = [
    "RouterFleet",
    "SshTunnel",
    "WorkerLink",
    "WorkerSpec",
    "parse_workers",
]
