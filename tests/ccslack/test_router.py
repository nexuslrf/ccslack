from types import SimpleNamespace

import pytest

from ccslack import router as router_mod
from ccslack.router import Router, channel_of, host_directive, route_and_dispatch


# --- host_directive -------------------------------------------------------


def test_host_directive_parses_flag():
    assert host_directive({"command": "/ccslack", "text": "new /p codex --host gpu1"}) == "gpu1"
    assert host_directive({"command": "/ccslack", "text": "new /p --host=gpu2"}) == "gpu2"


def test_host_directive_none_without_flag_or_command():
    assert host_directive({"command": "/ccslack", "text": "list"}) is None
    assert host_directive({"event": {"text": "--host x"}}) is None  # not a slash cmd


# --- channel_of -----------------------------------------------------------


def test_channel_of_slash_command():
    assert channel_of({"command": "/ccslack", "channel_id": "C1"}) == "C1"


def test_channel_of_block_actions():
    assert channel_of({"type": "block_actions", "channel": {"id": "C2"}}) == "C2"


def test_channel_of_events_message():
    payload = {"type": "event_callback", "event": {"type": "message", "channel": "C3"}}
    assert channel_of(payload) == "C3"


def test_channel_of_none_when_absent():
    assert channel_of({"type": "view_submission", "view": {}}) is None
    assert channel_of({}) is None


# --- Router registry + routing decision -----------------------------------


def test_unknown_channel_routes_local():
    r = Router(local_host="r0")
    assert r.target_host({"channel_id": "C1"}) is None


def test_remote_channel_routes_to_host():
    r = Router(local_host="r0")
    r.bind("gpu1", "C9")
    assert r.target_host({"channel": {"id": "C9"}}) == "gpu1"


def test_local_host_owned_channel_routes_local():
    r = Router(local_host="r0")
    r.bind("r0", "C5")  # owned by the router's own host
    assert r.target_host({"channel_id": "C5"}) is None


def test_unbind_and_drop_host():
    r = Router(local_host="r0")
    r.set_host_channels("gpu1", ["C1", "C2"])
    r.set_host_channels("gpu2", ["C3"])
    assert r.host_for_channel("C1") == "gpu1"
    r.unbind("gpu1", "C1")
    assert r.host_for_channel("C1") is None
    r.drop_host("gpu1")
    assert r.host_for_channel("C2") is None
    assert r.host_for_channel("C3") == "gpu2"  # other host untouched


def test_set_host_channels_replaces_prior_snapshot():
    r = Router(local_host="r0")
    r.set_host_channels("gpu1", ["C1", "C2"])
    r.set_host_channels("gpu1", ["C2", "C3"])  # C1 dropped, C3 added
    assert r.host_for_channel("C1") is None
    assert r.host_for_channel("C2") == "gpu1"
    assert r.host_for_channel("C3") == "gpu1"


def test_connected_hosts_tracking():
    r = Router(local_host="r0")
    r.set_host_channels("gpu1", [])  # zero-session worker still counts
    assert "gpu1" in r.connected_hosts
    r.drop_host("gpu1")
    assert "gpu1" not in r.connected_hosts


# --- --host routing -------------------------------------------------------


def test_host_directive_routes_to_connected_remote():
    r = Router(local_host="r0")
    r.set_host_channels("gpu1", [])
    payload = {"command": "/ccslack", "text": "new /p --host gpu1"}
    assert r.target_host(payload) == "gpu1"


def test_host_directive_local_routes_local():
    r = Router(local_host="r0")
    payload = {"command": "/ccslack", "text": "new /p --host r0"}
    assert r.target_host(payload) is None


def test_host_directive_unknown_routes_local():
    r = Router(local_host="r0")
    payload = {"command": "/ccslack", "text": "new /p --host ghost"}
    assert r.target_host(payload) is None  # local; _handle_new will report it


# --- route_and_dispatch: ack + local dispatch vs forward ------------------


class _FakeClient:
    def __init__(self) -> None:
        self.acks: list = []

    async def send_socket_mode_response(self, resp) -> None:  # noqa: ANN001
        self.acks.append(resp)


@pytest.mark.asyncio
async def test_route_and_dispatch_acks_and_dispatches_local(monkeypatch):
    r = Router(local_host="r0")
    dispatched: list = []

    async def _fake_dispatch(app, req):  # noqa: ANN001
        dispatched.append(req)

    monkeypatch.setattr(router_mod, "run_async_bolt_app", _fake_dispatch)

    client = _FakeClient()
    req = SimpleNamespace(
        envelope_id="e1", payload={"channel_id": "C1", "command": "/ccslack"}
    )
    await route_and_dispatch(client, req, app=object(), router=r)

    assert len(client.acks) == 1  # acked first
    assert len(dispatched) == 1  # unknown channel → local dispatch


@pytest.mark.asyncio
async def test_route_and_dispatch_forwards_remote_without_local_dispatch(monkeypatch):
    r = Router(local_host="r0")
    r.bind("gpu1", "C9")
    dispatched: list = []

    async def _fake_dispatch(app, req):  # noqa: ANN001
        dispatched.append(req)

    monkeypatch.setattr(router_mod, "run_async_bolt_app", _fake_dispatch)

    forwarded: list = []

    async def _fwd(host, payload) -> bool:  # noqa: ANN001
        forwarded.append((host, payload))
        return True

    r.set_forwarder(_fwd)

    client = _FakeClient()
    req = SimpleNamespace(envelope_id="e2", payload={"channel": {"id": "C9"}})
    await route_and_dispatch(client, req, app=object(), router=r)

    assert len(client.acks) == 1  # acked after a successful forward
    assert dispatched == []  # NOT dispatched locally
    assert forwarded == [("gpu1", {"channel": {"id": "C9"}})]


@pytest.mark.asyncio
async def test_route_and_dispatch_no_ack_when_forward_fails(monkeypatch):
    r = Router(local_host="r0")
    r.bind("gpu1", "C9")

    async def _fake_dispatch(app, req):  # noqa: ANN001
        raise AssertionError("must not dispatch locally")

    monkeypatch.setattr(router_mod, "run_async_bolt_app", _fake_dispatch)

    async def _fwd(host, payload) -> bool:  # noqa: ANN001
        return False  # worker link down

    r.set_forwarder(_fwd)

    client = _FakeClient()
    req = SimpleNamespace(envelope_id="e3", payload={"channel": {"id": "C9"}})
    await route_and_dispatch(client, req, app=object(), router=r)

    assert client.acks == []  # NOT acked → Slack redelivers
