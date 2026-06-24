import pytest

from ccslack import fleet_state
from ccslack.config import config
from ccslack.handlers.meta import (
    _handle_fleet,
    _handle_list,
    _handle_new,
    _handle_sessions,
)
from ccslack.router import Router
from ccslack.session import session_manager
from ccslack.slack_client import FakeSlackClient
from ccslack.thread_router import thread_router


@pytest.mark.asyncio
async def test_new_rejects_unavailable_host(monkeypatch):
    monkeypatch.setattr(config, "host_name", "r0")
    r = Router(local_host="r0")
    r.set_host_channels("gpu1", [])
    fleet_state.install_router(r)
    client = FakeSlackClient()

    # --host names a host that isn't this one (router didn't forward → unknown).
    await _handle_new(client, "C0META", "U1", ["/proj", "codex", "--host", "ghost"])

    eph = client.last_call("chat_postEphemeral")
    assert eph is not None
    assert "isn't available" in eph.kwargs["text"]
    assert "`gpu1`" in eph.kwargs["text"] and "`r0`" in eph.kwargs["text"]
    # Nothing spawned.
    assert client.call_count("conversations_create") == 0


@pytest.mark.asyncio
async def test_new_host_matching_local_proceeds(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "host_name", "gpu1")
    spawned = {}

    async def _fake_create_window(**kwargs):
        spawned.update(kwargs)
        return (False, "tmux unavailable in test", "", "")

    monkeypatch.setattr(
        "ccslack.handlers.meta.tmux_manager.create_window", _fake_create_window
    )
    client = FakeSlackClient()

    # --host == this host: strips the flag and proceeds (tmux create then fails
    # in-test, but we got past the host gate and into spawning).
    await _handle_new(client, "C0META", "U1", [str(tmp_path), "shell", "--host", "gpu1"])

    assert spawned.get("work_dir") == str(tmp_path)


@pytest.mark.asyncio
async def test_fleet_shows_per_host_status(monkeypatch):
    monkeypatch.setattr(config, "host_name", "r0")
    r = Router(local_host="r0")
    r.set_host_channels("gpu1", ["C1"])
    fleet_state.install_router(r)
    fleet_state.set_workers([("gpu1", "user@gpu1"), ("gpu2", "gpu2-alias")])
    client = FakeSlackClient()

    await _handle_fleet(client, "C0META", "U1")

    text = client.last_call("chat_postEphemeral").kwargs["text"]
    assert "Fleet status" in text
    assert "`r0`" in text and "`gpu1`" in text and "`gpu2`" in text
    assert "disconnected" in text  # gpu2 never connected


@pytest.mark.asyncio
async def test_fleet_not_a_router(monkeypatch):
    fleet_state.reset()
    client = FakeSlackClient()
    await _handle_fleet(client, "C0META", "U1")
    assert "not a multi-host router" in client.last_call("chat_postEphemeral").kwargs["text"]


@pytest.mark.asyncio
async def test_list_shows_local_and_remote(monkeypatch):
    monkeypatch.setattr(config, "host_name", "r0")
    r = Router(local_host="r0")
    r.set_host_channels("gpu1", ["C_REMOTE"])
    fleet_state.install_router(r)

    session_manager.set_window_provider("@1", "claude", cwd="/local")
    thread_router.bind_channel("C_LOCAL", "@1", window_name="loc")

    client = FakeSlackClient()
    await _handle_list(client, "C0META", "U1")

    text = client.last_call("chat_postEphemeral").kwargs["text"]
    assert "C_LOCAL" in text  # local session
    assert "C_REMOTE" in text  # remote channel
    assert "host `gpu1`" in text


def _kill_values(blocks: list[dict]) -> list[str]:
    vals = []
    for b in blocks:
        acc = b.get("accessory", {})
        if acc.get("action_id") == "ccslack_dashboard_kill":
            vals.append(acc["value"])
    return vals


@pytest.mark.asyncio
async def test_sessions_merges_local_and_remote(monkeypatch):
    monkeypatch.setattr(config, "host_name", "r0")
    r = Router(local_host="r0")
    fleet_state.install_router(r)

    async def _gather():
        return [
            {
                "channel": "C_REMOTE",
                "window": "@9",
                "provider": "codex",
                "cwd": "/r",
                "display": "rem",
                "state": "idle",
                "host": "gpu1",
            }
        ]

    fleet_state.set_session_gatherer(_gather)

    session_manager.set_window_provider("@1", "claude", cwd="/l")
    thread_router.bind_channel("C_LOCAL", "@1", window_name="loc")

    client = FakeSlackClient()
    await _handle_sessions(client, "C0META", "U1")

    call = client.last_call("chat_postEphemeral")
    blocks = call.kwargs["blocks"]
    text_blob = str(blocks)
    assert "C_LOCAL" in text_blob and "C_REMOTE" in text_blob
    assert "`gpu1`" in text_blob  # remote row tagged with host
    # Local kill value has 2 parts; remote has 3 (carries host for forwarding).
    vals = _kill_values(blocks)
    assert "C_LOCAL|@1" in vals
    assert "C_REMOTE|@9|gpu1" in vals
