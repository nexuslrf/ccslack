import pytest

from ccslack.handlers.meta import (
    _PENDING_RELAUNCH,
    _handle_relaunch,
    _relaunch_cmd,
)
from ccslack.session import session_manager
from ccslack.slack_client import FakeSlackClient
from ccslack.thread_router import thread_router
from ccslack.window_state_store import window_store


def _bind(
    channel_id: str, window_id: str, provider: str = "claude", cwd: str = "/proj"
) -> None:
    session_manager.set_window_provider(window_id, provider, cwd=cwd)
    thread_router.bind_channel(channel_id, window_id, window_name="proj")


@pytest.fixture(autouse=True)
def _clean():
    window_store.window_states.clear()
    thread_router.reset()
    _PENDING_RELAUNCH.clear()
    yield
    window_store.window_states.clear()
    thread_router.reset()
    _PENDING_RELAUNCH.clear()


def _live_window(monkeypatch):
    async def _live(_wid):
        return object()

    monkeypatch.setattr(
        "ccslack.handlers.meta.tmux_manager.find_window_by_id", _live
    )


def test_relaunch_cmd_continue_appends_custom_args():
    cmd = _relaunch_cmd("claude", "sid1", ["--model", "opus"], fresh=False)
    assert cmd == "claude --continue --model opus"


def test_relaunch_cmd_fresh_omits_continue():
    cmd = _relaunch_cmd("claude", "sid1", ["--model", "opus"], fresh=True)
    assert cmd == "claude --model opus"


def test_relaunch_cmd_quotes_multiword_and_neutralizes_injection():
    cmd = _relaunch_cmd("claude", "", ["--append-system-prompt", "be terse"], fresh=True)
    assert cmd == "claude --append-system-prompt 'be terse'"
    injected = _relaunch_cmd("claude", "", ["; rm -rf /"], fresh=True)
    assert injected == "claude '; rm -rf /'"


@pytest.mark.asyncio
async def test_relaunch_posts_confirm_and_stores_pending(monkeypatch):
    _bind("C0R1", "@20", provider="claude")
    _live_window(monkeypatch)
    client = FakeSlackClient()

    await _handle_relaunch(client, "C0R1", "U1", ["--model", "opus"])

    msg = client.last_call("chat_postMessage")
    assert msg is not None
    blocks_text = str(msg.kwargs.get("blocks", ""))
    assert "claude --continue --model opus" in blocks_text
    assert "ccslack_relaunch_confirm" in blocks_text
    assert _PENDING_RELAUNCH["@20"] == "claude --continue --model opus"


@pytest.mark.asyncio
async def test_relaunch_fresh_flag(monkeypatch):
    _bind("C0R2", "@21", provider="claude")
    _live_window(monkeypatch)
    client = FakeSlackClient()

    await _handle_relaunch(client, "C0R2", "U1", ["--fresh", "--model", "opus"])

    assert _PENDING_RELAUNCH["@21"] == "claude --model opus"
    blocks_text = str(client.last_call("chat_postMessage").kwargs.get("blocks", ""))
    assert "fresh" in blocks_text.lower()


@pytest.mark.asyncio
async def test_relaunch_rejects_unbound_channel():
    client = FakeSlackClient()

    await _handle_relaunch(client, "C0UNBOUND", "U1", ["--model", "opus"])

    assert client.call_count("chat_postMessage") == 0
    eph = client.last_call("chat_postEphemeral")
    assert "bound session channel" in eph.kwargs["text"]


@pytest.mark.asyncio
async def test_relaunch_rejects_dead_window(monkeypatch):
    _bind("C0R3", "@22", provider="claude")

    async def _dead(_wid):
        return None

    monkeypatch.setattr(
        "ccslack.handlers.meta.tmux_manager.find_window_by_id", _dead
    )
    client = FakeSlackClient()

    await _handle_relaunch(client, "C0R3", "U1", ["--model", "opus"])

    assert client.call_count("chat_postMessage") == 0
    assert "restore" in client.last_call("chat_postEphemeral").kwargs["text"]


@pytest.mark.asyncio
async def test_relaunch_rejects_multiline_args(monkeypatch):
    _bind("C0R4", "@23", provider="claude")
    _live_window(monkeypatch)
    client = FakeSlackClient()

    await _handle_relaunch(client, "C0R4", "U1", ["--x", "line1\nline2"])

    assert client.call_count("chat_postMessage") == 0
    assert "single line" in client.last_call("chat_postEphemeral").kwargs["text"]
    assert "@23" not in _PENDING_RELAUNCH


@pytest.mark.asyncio
async def test_relaunch_no_args_still_confirms_bare_continue(monkeypatch):
    _bind("C0R5", "@24", provider="claude")
    _live_window(monkeypatch)
    client = FakeSlackClient()

    await _handle_relaunch(client, "C0R5", "U1", [])

    assert _PENDING_RELAUNCH["@24"] == "claude --continue"
