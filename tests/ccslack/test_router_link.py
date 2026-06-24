import asyncio
import time

import pytest
from slack_bolt.async_app import AsyncApp
from slack_bolt.authorization import AuthorizeResult

from ccslack.event_source import RouterLinkSource
from ccslack.router import Router
from ccslack.router_link import RouterFleet, SshTunnel, WorkerLink, parse_workers
from ccslack.thread_router import thread_router

_MESSAGE = {
    "token": "x",
    "team_id": "T1",
    "type": "event_callback",
    "event": {
        "type": "message",
        "channel": "C1",
        "channel_type": "channel",
        "user": "U1",
        "text": "routed!",
        "ts": "1700.0001",
    },
    "event_id": "Ev1",
    "event_time": 1,
    "authorizations": [
        {"enterprise_id": None, "team_id": "T1", "user_id": "U0BOT", "is_bot": True}
    ],
}


async def _authorize(*_a, **_k) -> AuthorizeResult:
    return AuthorizeResult(
        enterprise_id=None, team_id="T1", bot_token="xoxb-fake", bot_id="B1", bot_user_id="U0BOT"
    )


def _app() -> AsyncApp:
    return AsyncApp(
        authorize=_authorize,
        request_verification_enabled=False,
        raise_error_for_unhandled_request=False,
    )


async def _settle(check, timeout: float = 2.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if check():
            return
        await asyncio.sleep(0.01)


@pytest.fixture(autouse=True)
def _clean():
    thread_router.reset()
    yield
    thread_router.reset()


# --- parse_workers --------------------------------------------------------


def test_parse_workers_basic():
    specs = parse_workers("gpu1=user@gpu1, gpu2=gpu2-alias", 8765)
    assert [(s.host, s.ssh_target, s.remote_port) for s in specs] == [
        ("gpu1", "user@gpu1", 8765),
        ("gpu2", "gpu2-alias", 8765),
    ]


def test_parse_workers_skips_malformed_and_dups():
    specs = parse_workers("gpu1=t1,broken,=t2,gpu3=,gpu1=again", 9000)
    assert [s.host for s in specs] == ["gpu1"]


def test_ssh_tunnel_command_shape():
    tunnel = SshTunnel("user@gpu1", remote_port=8765)
    cmd = tunnel.build_command()
    assert cmd[0] == "ssh" and cmd[-1] == "user@gpu1"
    assert "-N" in cmd
    joined = " ".join(cmd)
    assert f"{tunnel.local_port}:127.0.0.1:8765" in joined
    assert "ServerAliveInterval=15" in joined


# --- RouterFleet forwarder ------------------------------------------------


@pytest.mark.asyncio
async def test_fleet_forward_unknown_host_returns_false():
    fleet = RouterFleet(Router("r0"), [])
    assert await fleet._forward("nope", {"x": 1}) is False


# --- WorkerLink <-> RouterLinkSource integration (no SSH) ------------------


@pytest.mark.asyncio
async def test_worker_link_registry_forward_and_disconnect():
    # Worker side: a session channel already owned before the router connects.
    thread_router.bind_channel("C1", "@1")
    worker_app = _app()
    seen: list[dict] = []

    @worker_app.event("message")
    async def _on(event) -> None:  # noqa: ANN001
        seen.append(event)

    worker = RouterLinkSource(worker_app, host="gpu1", port=0)
    await worker.start()

    router = Router(local_host="r0")

    async def _connect():
        return await asyncio.open_connection("127.0.0.1", worker.port)

    worker_link = WorkerLink("gpu1", _connect, router)
    await worker_link.start()
    try:
        # hello snapshot populates the registry
        await _settle(lambda: router.host_for_channel("C1") == "gpu1")
        assert router.target_host({"channel_id": "C1"}) == "gpu1"

        # forwarded event is dispatched into the worker's app
        assert await worker_link.forward(_MESSAGE) is True
        await _settle(lambda: len(seen) == 1)
        assert seen[0]["text"] == "routed!"

        # live bind/unbind propagate to the router registry
        thread_router.bind_channel("C2", "@2")
        await _settle(lambda: router.host_for_channel("C2") == "gpu1")
        thread_router.unbind_channel("C2")
        await _settle(lambda: router.host_for_channel("C2") is None)

        # worker disconnect drops the host's channels
        await worker.stop()
        await _settle(lambda: router.host_for_channel("C1") is None)
        assert await worker_link.forward(_MESSAGE) is False  # no live link
    finally:
        await worker_link.stop()
        await worker.stop()


@pytest.mark.asyncio
async def test_sessions_rpc_over_link(monkeypatch):
    from ccslack.session import session_manager

    # Worker owns one session with detail.
    session_manager.set_window_provider("@7", "codex", cwd="/work/proj")
    thread_router.bind_channel("Cws", "@7", window_name="proj")

    worker_app = _app()
    worker = RouterLinkSource(worker_app, host="gpu1", port=0)
    await worker.start()

    router = Router(local_host="r0")

    async def _connect():
        return await asyncio.open_connection("127.0.0.1", worker.port)

    worker_link = WorkerLink("gpu1", _connect, router)
    await worker_link.start()
    try:
        await _settle(lambda: router.host_for_channel("Cws") == "gpu1")
        rows = await worker_link.request_sessions(timeout=2.0)
        assert len(rows) == 1
        assert rows[0]["channel"] == "Cws"
        assert rows[0]["provider"] == "codex"
        assert rows[0]["cwd"] == "/work/proj"
    finally:
        await worker_link.stop()
        await worker.stop()


@pytest.mark.asyncio
async def test_request_sessions_returns_empty_when_link_down():
    worker_link = WorkerLink("gpu1", None, Router("r0"))  # never started
    assert await worker_link.request_sessions(timeout=0.2) == []


@pytest.mark.asyncio
async def test_worker_link_notifies_connect_and_disconnect():
    worker_app = _app()
    worker = RouterLinkSource(worker_app, host="gpu1", port=0)
    await worker.start()

    notes: list[str] = []

    async def _notify(text: str) -> None:
        notes.append(text)

    async def _connect():
        return await asyncio.open_connection("127.0.0.1", worker.port)

    worker_link = WorkerLink("gpu1", _connect, Router("r0"), notify=_notify)
    await worker_link.start()
    try:
        await _settle(lambda: any("connected" in n for n in notes))
        # Worker goes away → a disconnect note fires.
        await worker.stop()
        await _settle(lambda: any("disconnected" in n for n in notes))
    finally:
        await worker_link.stop()
        await worker.stop()

    assert any("connected" in n for n in notes)
    assert any("disconnected" in n for n in notes)
