import pytest

from ccslack import window_query
from ccslack.config import config
from ccslack.handlers.messaging_pipeline.message_routing import _pre_post_suppressed
from ccslack.handlers.meta import _handle_toolcalls
from ccslack.monitor_events import NewMessage
from ccslack.session import session_manager
from ccslack.slack_client import FakeSlackClient
from ccslack.thread_router import thread_router
from ccslack.window_state_store import (
    WindowState,
    normalize_tool_call_visibility,
    window_store,
)


@pytest.fixture(autouse=True)
def _clean():
    window_store.window_states.clear()
    thread_router.reset()
    yield
    window_store.window_states.clear()
    thread_router.reset()


def _bind(channel: str, window: str) -> None:
    session_manager.set_window_provider(window, "codex", cwd="/x")
    thread_router.bind_channel(channel, window, window_name="p")


def _tool(content_type: str, tool_id: str = "t1") -> NewMessage:
    return NewMessage(
        session_id="s1",
        text="bash: ls" if content_type == "tool_use" else "exit 0\nfiles...",
        is_complete=True,
        content_type=content_type,
        tool_use_id=tool_id,
        tool_name="bash",
    )


async def _suppressed(window: str, msg: NewMessage, monkeypatch) -> bool:
    async def _noop_status(*_a, **_k):
        return None

    monkeypatch.setattr("ccslack.handlers.status.update_status", _noop_status)
    return await _pre_post_suppressed(FakeSlackClient(), "C1", window, msg, msg.text)


# ── defaults & normalisation ───────────────────────────────────────────────


def test_global_default_is_calls():
    assert config.toolcall_detail == "calls"


def test_legacy_shown_normalises_to_full():
    assert normalize_tool_call_visibility("shown") == "full"
    window_store.set_tool_call_visibility("@1", "shown")
    assert window_store.get_tool_call_visibility("@1") == "full"


def test_default_resolves_to_global():
    _bind("C1", "@1")  # no per-window override → default
    assert window_query.resolved_toolcall_detail("@1") == "calls"


def test_persisted_shown_round_trips_as_full():
    state = WindowState(session_id="s", cwd="/x")
    state.tool_call_visibility = "shown"
    restored = WindowState.from_dict(state.to_dict())
    # Stored value may be legacy, but the accessor normalises it.
    window_store.window_states["@9"] = restored
    assert window_store.get_tool_call_visibility("@9") == "full"


# ── routing: what actually posts ───────────────────────────────────────────


@pytest.mark.asyncio
async def test_calls_skips_result_keeps_call(monkeypatch):
    _bind("C1", "@1")
    session_manager.set_tool_call_visibility("@1", "calls")
    assert await _suppressed("@1", _tool("tool_use"), monkeypatch) is False  # call posts
    assert await _suppressed("@1", _tool("tool_result"), monkeypatch) is True  # result dropped


@pytest.mark.asyncio
async def test_full_keeps_both(monkeypatch):
    _bind("C1", "@1")
    session_manager.set_tool_call_visibility("@1", "full")
    assert await _suppressed("@1", _tool("tool_use"), monkeypatch) is False
    assert await _suppressed("@1", _tool("tool_result"), monkeypatch) is False


@pytest.mark.asyncio
async def test_hidden_drops_both(monkeypatch):
    _bind("C1", "@1")
    session_manager.set_tool_call_visibility("@1", "hidden")
    assert await _suppressed("@1", _tool("tool_use"), monkeypatch) is True
    assert await _suppressed("@1", _tool("tool_result"), monkeypatch) is True


@pytest.mark.asyncio
async def test_default_window_uses_global_calls(monkeypatch):
    _bind("C1", "@1")  # default → global "calls"
    assert await _suppressed("@1", _tool("tool_use"), monkeypatch) is False
    assert await _suppressed("@1", _tool("tool_result"), monkeypatch) is True


# ── command ─────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_command_sets_calls():
    _bind("C1", "@1")
    client = FakeSlackClient()
    await _handle_toolcalls(client, "C1", "U1", ["calls"])
    assert window_store.get_tool_call_visibility("@1") == "calls"
    assert "exec result skipped" in client.last_call("chat_postEphemeral").kwargs["text"]


@pytest.mark.asyncio
async def test_command_accepts_legacy_shown():
    _bind("C1", "@1")
    client = FakeSlackClient()
    await _handle_toolcalls(client, "C1", "U1", ["shown"])
    assert window_store.get_tool_call_visibility("@1") == "full"


@pytest.mark.asyncio
async def test_command_cycles_full_calls_hidden_default():
    _bind("C1", "@1")
    client = FakeSlackClient()
    seen = []
    for _ in range(4):
        await _handle_toolcalls(client, "C1", "U1", [])
        seen.append(window_store.get_tool_call_visibility("@1"))
    assert seen == ["full", "calls", "hidden", "default"]


@pytest.mark.asyncio
async def test_command_rejects_bad_arg():
    _bind("C1", "@1")
    client = FakeSlackClient()
    await _handle_toolcalls(client, "C1", "U1", ["sometimes"])
    assert "unknown mode" in client.last_call("chat_postEphemeral").kwargs["text"]
