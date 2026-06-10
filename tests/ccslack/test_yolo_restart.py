import pytest

from ccslack.handlers.meta import _handle_yolo
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
    yield
    window_store.window_states.clear()
    thread_router.reset()


@pytest.mark.asyncio
async def test_yolo_posts_confirm_message_for_claude(monkeypatch):
    _bind("C0Y1", "@10", provider="claude")
    client = FakeSlackClient()

    async def _live(wid):
        return object()

    monkeypatch.setattr(
        "ccslack.handlers.meta.tmux_manager.find_window_by_id",
        _live,
    )

    await _handle_yolo(client, "C0Y1", "U1", [])

    msg = client.last_call("chat_postMessage")
    assert msg is not None
    assert "YOLO" in msg.kwargs["text"]
    # Should include the full launch command in the block
    blocks_text = str(msg.kwargs.get("blocks", ""))
    assert "--dangerously-skip-permissions" in blocks_text
    assert "--continue" in blocks_text


@pytest.mark.asyncio
async def test_yolo_posts_confirm_message_for_codex(monkeypatch):
    _bind("C0Y2", "@11", provider="codex")
    client = FakeSlackClient()

    async def _live(wid):
        return object()

    monkeypatch.setattr(
        "ccslack.handlers.meta.tmux_manager.find_window_by_id",
        _live,
    )

    await _handle_yolo(client, "C0Y2", "U1", [])

    msg = client.last_call("chat_postMessage")
    assert msg is not None
    blocks_text = str(msg.kwargs.get("blocks", ""))
    assert "--dangerously-bypass-approvals-and-sandbox" in blocks_text


@pytest.mark.asyncio
async def test_yolo_rejects_unbound_channel():
    client = FakeSlackClient()

    await _handle_yolo(client, "C0UNBOUND", "U1", [])

    assert client.call_count("chat_postMessage") == 0
    eph = client.last_call("chat_postEphemeral")
    assert eph is not None
    assert "bound session channel" in eph.kwargs["text"]


@pytest.mark.asyncio
async def test_yolo_rejects_dead_window(monkeypatch):
    _bind("C0Y3", "@12", provider="claude")
    client = FakeSlackClient()

    async def _dead(wid):
        return None

    monkeypatch.setattr(
        "ccslack.handlers.meta.tmux_manager.find_window_by_id",
        _dead,
    )

    await _handle_yolo(client, "C0Y3", "U1", [])

    assert client.call_count("chat_postMessage") == 0
    eph = client.last_call("chat_postEphemeral")
    assert "dead" in eph.kwargs["text"].lower() or "restore" in eph.kwargs["text"]


@pytest.mark.asyncio
async def test_yolo_rejects_unsupported_provider(monkeypatch):
    _bind("C0Y4", "@13", provider="shell")
    client = FakeSlackClient()

    async def _live(wid):
        return object()

    monkeypatch.setattr(
        "ccslack.handlers.meta.tmux_manager.find_window_by_id",
        _live,
    )

    await _handle_yolo(client, "C0Y4", "U1", [])

    assert client.call_count("chat_postMessage") == 0
    eph = client.last_call("chat_postEphemeral")
    assert "YOLO" in eph.kwargs["text"]


@pytest.mark.asyncio
async def test_yolo_allows_reyolo_when_flag_already_set(monkeypatch):
    # The persisted approval_mode flag is unreliable (it drifts when the agent
    # restarts outside this flow), so /ccslack yolo must still offer to restart
    # even when the window is already flagged yolo.
    _bind("C0Y5", "@14", provider="claude")
    session_manager.set_window_approval_mode("@14", "yolo")
    client = FakeSlackClient()

    async def _live(wid):
        return object()

    monkeypatch.setattr(
        "ccslack.handlers.meta.tmux_manager.find_window_by_id",
        _live,
    )

    await _handle_yolo(client, "C0Y5", "U1", [])

    assert client.call_count("chat_postEphemeral") == 0
    msg = client.last_call("chat_postMessage")
    assert msg is not None
    assert "YOLO" in msg.kwargs["text"]


@pytest.mark.asyncio
async def test_yolo_off_posts_normal_confirm(monkeypatch):
    _bind("C0Y6", "@15", provider="claude")
    session_manager.set_window_approval_mode("@15", "yolo")
    client = FakeSlackClient()

    async def _live(wid):
        return object()

    monkeypatch.setattr(
        "ccslack.handlers.meta.tmux_manager.find_window_by_id",
        _live,
    )

    await _handle_yolo(client, "C0Y6", "U1", ["off"])

    msg = client.last_call("chat_postMessage")
    assert msg is not None
    assert "normal" in msg.kwargs["text"]
    blocks_text = str(msg.kwargs.get("blocks", ""))
    # Normal relaunch must NOT carry the skip-approvals flag, but keeps continue.
    assert "--dangerously-skip-permissions" not in blocks_text
    assert "--continue" in blocks_text
    assert "ccslack_yolo_confirm" in blocks_text
    assert "@15|normal" in blocks_text


@pytest.mark.asyncio
async def test_yolo_off_works_even_without_yolo_capable_provider(monkeypatch):
    # Switching *to* normal must not require YOLO capability.
    _bind("C0Y7", "@16", provider="shell")
    client = FakeSlackClient()

    async def _live(wid):
        return object()

    monkeypatch.setattr(
        "ccslack.handlers.meta.tmux_manager.find_window_by_id",
        _live,
    )

    await _handle_yolo(client, "C0Y7", "U1", ["off"])

    assert client.call_count("chat_postEphemeral") == 0
    assert client.last_call("chat_postMessage") is not None


@pytest.mark.asyncio
async def test_yolo_rejects_unknown_arg(monkeypatch):
    _bind("C0Y8", "@17", provider="claude")
    client = FakeSlackClient()

    async def _live(wid):
        return object()

    monkeypatch.setattr(
        "ccslack.handlers.meta.tmux_manager.find_window_by_id",
        _live,
    )

    await _handle_yolo(client, "C0Y8", "U1", ["maybe"])

    assert client.call_count("chat_postMessage") == 0
    eph = client.last_call("chat_postEphemeral")
    assert eph is not None
    assert "usage" in eph.kwargs["text"]
