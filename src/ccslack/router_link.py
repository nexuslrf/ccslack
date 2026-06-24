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
# Called with (host, prompt_text, options) when an SSH tunnel needs interactive
# auth (e.g. Duo). ``options`` is the parsed numbered choices.
OnPrompt = Callable[[str, str, "list[tuple[str, str]]"], Awaitable[None]]

# Cap the captured PTY buffer so it can't grow without bound between prompts.
_PTY_BUF_MAX = 8000


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
    """A supervised ``ssh -N -L`` tunnel to one worker's link server.

    By default the ``ssh`` subprocess inherits the router's stdio, so any
    interactive auth is answered at the console. When ``CCSLACK_SSH_INTERACTIVE``
    is on and an ``on_prompt`` callback is given, it instead runs under a PTY and
    bridges auth prompts to Slack (see :mod:`ccslack.ssh_auth`).
    """

    def __init__(
        self,
        ssh_target: str,
        remote_port: int,
        *,
        host: str = "",
        on_prompt: OnPrompt | None = None,
    ) -> None:
        self.ssh_target = ssh_target
        self.remote_port = remote_port
        self.host = host
        self._on_prompt = on_prompt
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

    def _interactive(self) -> bool:
        from .config import config

        return config.ssh_interactive and self._on_prompt is not None

    async def _run_pty(self, cmd: list[str]) -> None:
        """Run ssh under a PTY, bridging auth prompts to ``on_prompt``."""
        from . import ssh_auth
        from .config import config

        buf = ""
        last_fired = ""
        fired_any = False
        seen = ""  # everything captured, for diagnostics on exit

        async def _on_output(chunk: str) -> None:
            nonlocal buf, last_fired, fired_any, seen
            clean = ssh_auth.strip_ansi(chunk)
            seen = (seen + clean)[-_PTY_BUF_MAX:]
            buf = (buf + clean)[-_PTY_BUF_MAX:]
            logger.debug("ssh[%s] pty: %r", self.host, clean[-200:])
            if (
                ssh_auth.looks_like_prompt(buf, config.ssh_prompt_re)
                and buf != last_fired
            ):
                last_fired = buf
                fired_any = True
                logger.info("ssh[%s]: auth prompt detected → posting to Slack", self.host)
                if self._on_prompt is not None:
                    await self._on_prompt(
                        self.host, buf.strip(), ssh_auth.parse_options(buf)
                    )

        pty = ssh_auth.PtyProcess(cmd, _on_output)

        async def _respond(text: str) -> bool:
            nonlocal buf, last_fired
            buf = ""
            last_fired = ""
            return await pty.write(text)

        ssh_auth.register_responder(self.host, _respond)
        try:
            await pty.start()
            code = await pty.wait()
        finally:
            ssh_auth.unregister_responder(self.host)
            await pty.stop()
        # Diagnostics: if ssh exited without us ever detecting a prompt, surface
        # what it printed so the prompt regex can be tuned (or the real error seen).
        tail = seen.strip()[-600:]
        if not fired_any and tail:
            logger.warning(
                "ssh[%s] exited (code=%s) with no prompt match. "
                "Captured output (tune CCSLACK_SSH_PROMPT_RE if this is a prompt):\n%s",
                self.host,
                code,
                tail,
            )
        elif not fired_any:
            logger.warning(
                "ssh[%s] exited (code=%s) producing no PTY output — likely a "
                "connection/auth failure before any prompt.",
                self.host,
                code,
            )

    async def _supervise(self) -> None:
        backoff = _BACKOFF_START
        while not self._stopping:
            cmd = self.build_command()
            logger.info("ssh tunnel up: %s (local :%d)", self.ssh_target, self.local_port)
            try:
                if self._interactive():
                    # PTY-bridge interactive auth (Duo/2FA) to Slack.
                    await self._run_pty(cmd)
                else:
                    # Inherit stdio so an operator at the router console can
                    # answer an interactive auth prompt (OTP) when SSH needs it.
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
        # Outstanding sessions RPCs: request id -> Future[list[rows]].
        self._pending: dict[int, asyncio.Future[list[dict[str, Any]]]] = {}
        self._req_seq = 0

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

    async def request_sessions(self, timeout: float) -> list[dict[str, Any]]:
        """Ask the worker for its sessions. Returns [] on down link / timeout."""
        writer = self._writer
        if writer is None:
            return []
        self._req_seq += 1
        req_id = self._req_seq
        future: asyncio.Future[list[dict[str, Any]]] = asyncio.get_event_loop().create_future()
        self._pending[req_id] = future
        try:
            await link.write_msg(writer, {"t": link.SESSIONS_REQ, "id": req_id})
            return await asyncio.wait_for(future, timeout=timeout)
        except (ConnectionError, OSError, asyncio.TimeoutError):
            return []
        finally:
            self._pending.pop(req_id, None)

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
                # Fail any in-flight RPCs so callers don't hang to timeout.
                for future in self._pending.values():
                    if not future.done():
                        future.set_result([])
                self._pending.clear()
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
        elif tag == link.SESSIONS_REP:
            req_id = msg.get("id")
            future = self._pending.get(req_id) if isinstance(req_id, int) else None
            if future is not None and not future.done():
                rows = msg.get("sessions")
                future.set_result(rows if isinstance(rows, list) else [])
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
        on_prompt: OnPrompt | None = None,
    ) -> None:
        self._router = router
        self._workers = workers
        self._notify = notify
        self._on_prompt = on_prompt
        self._tunnels: dict[str, SshTunnel] = {}
        self._links: dict[str, WorkerLink] = {}

    async def start(self) -> None:
        for spec in self._workers:
            tunnel = SshTunnel(
                spec.ssh_target,
                spec.remote_port,
                host=spec.host,
                on_prompt=self._on_prompt,
            )
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

    async def gather_sessions(self, timeout: float = 3.0) -> list[dict[str, Any]]:
        """Query every connected worker for its sessions, tagged by host."""
        links = list(self._links.items())
        if not links:
            return []
        results = await asyncio.gather(
            *(wl.request_sessions(timeout) for _host, wl in links)
        )
        rows: list[dict[str, Any]] = []
        for (host, _wl), host_rows in zip(links, results, strict=True):
            for row in host_rows:
                rows.append({**row, "host": host})
        return rows


__all__ = [
    "RouterFleet",
    "SshTunnel",
    "WorkerLink",
    "WorkerSpec",
    "parse_workers",
]
