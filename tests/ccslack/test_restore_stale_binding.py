import pytest

from ccslack.handlers.meta import _handle_restore
from ccslack.slack_client import FakeSlackClient
from ccslack.thread_router import thread_router
from ccslack.tmux_manager import TmuxWindow
from ccslack.window_state_store import window_store


@pytest.fixture(autouse=True)
def _clean():
    window_store.window_states.clear()
    thread_router.reset()
    yield
    window_store.window_states.clear()
    thread_router.reset()


def _topic(value: str) -> dict:
    return {"ok": True, "channel": {"topic": {"value": value}}}


@pytest.mark.asyncio
async def test_restore_readopts_when_live_window_cwd_disagrees_with_topic(monkeypatch):
    thread_router.bind_channel("C0STALE", "@70", window_name="active-gs")

    client = FakeSlackClient()
    client.returns["conversations_info"] = _topic(
        "codex · /nvmepool/ruofan/projects_leo/active-gs"
    )

    async def _live(wid):
        return TmuxWindow(
            window_id="@70", window_name="ruofan-2", cwd="/nvmepool/ruofan"
        )

    monkeypatch.setattr("ccslack.handlers.meta.tmux_manager.find_window_by_id", _live)

    spawned = []

    async def _fake_restore_in_channel(c, ch, *, provider, cwd, old_window_id, **kw):
        spawned.append((provider, cwd, old_window_id))
        return "@270"

    monkeypatch.setattr(
        "ccslack.handlers.recovery.restore_in_channel", _fake_restore_in_channel
    )

    await _handle_restore(client, "C0STALE", "U1", [])

    assert spawned == [("codex", "/nvmepool/ruofan/projects_leo/active-gs", None)]
    assert thread_router.get_window_for_channel("C0STALE") is None
    assert client.call_count("chat_postEphemeral") == 0


@pytest.mark.asyncio
async def test_restore_refuses_when_live_window_cwd_matches_topic(monkeypatch):
    thread_router.bind_channel("C0ALIVE", "@71", window_name="ruofan")

    client = FakeSlackClient()
    client.returns["conversations_info"] = _topic("claude · /nvmepool/ruofan")

    async def _live(wid):
        return TmuxWindow(window_id="@71", window_name="ruofan", cwd="/nvmepool/ruofan")

    monkeypatch.setattr("ccslack.handlers.meta.tmux_manager.find_window_by_id", _live)

    called = []

    async def _fake_restore_in_channel(*a, **kw):
        called.append((a, kw))
        return "@999"

    monkeypatch.setattr(
        "ccslack.handlers.recovery.restore_in_channel", _fake_restore_in_channel
    )

    await _handle_restore(client, "C0ALIVE", "U1", [])

    assert called == []
    eph = client.last_call("chat_postEphemeral")
    assert eph is not None
    assert "still alive" in eph.kwargs["text"]


@pytest.mark.asyncio
async def test_restore_refuses_live_window_when_topic_unreadable(monkeypatch):
    thread_router.bind_channel("C0NOPIC", "@72", window_name="proj")

    client = FakeSlackClient()
    client.returns["conversations_info"] = _topic("")

    async def _live(wid):
        return TmuxWindow(window_id="@72", window_name="proj", cwd="/some/where")

    monkeypatch.setattr("ccslack.handlers.meta.tmux_manager.find_window_by_id", _live)

    await _handle_restore(client, "C0NOPIC", "U1", [])

    eph = client.last_call("chat_postEphemeral")
    assert eph is not None
    assert "still alive" in eph.kwargs["text"]
    assert thread_router.get_window_for_channel("C0NOPIC") == "@72"
