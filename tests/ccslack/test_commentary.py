import pytest

from ccslack import window_query
from ccslack.handlers.messaging_pipeline.message_routing import (
    _buffer_suppressed_answer,
    _decorate,
    _pre_post_suppressed,
)
from ccslack.handlers.meta import _handle_commentary
from ccslack.handlers import mute_buffer
from ccslack.monitor_events import NewMessage
from ccslack.providers.codex import CodexProvider
from ccslack.session import session_manager
from ccslack.slack_client import FakeSlackClient
from ccslack.thread_router import thread_router
from ccslack.window_state_store import WindowState, window_store


@pytest.fixture(autouse=True)
def _clean():
    window_store.window_states.clear()
    thread_router.reset()
    mute_buffer.reset()
    yield
    window_store.window_states.clear()
    thread_router.reset()
    mute_buffer.reset()


def _bind(channel: str, window: str) -> None:
    session_manager.set_window_provider(window, "codex", cwd="/x")
    thread_router.bind_channel(channel, window, window_name="p")


def _commentary(text: str = "First, checking the repo…") -> NewMessage:
    return NewMessage(
        session_id="s1", text=text, is_complete=True, content_type="text",
        phase="commentary",
    )


def _final(text: str = "Done — build succeeded.") -> NewMessage:
    return NewMessage(
        session_id="s1", text=text, is_complete=True, content_type="text",
        phase="final_answer",
    )


# ── codex parse captures the phase ─────────────────────────────────────────


def test_codex_agent_message_carries_commentary_phase():
    p = CodexProvider()
    entries = [
        {"type": "event_msg", "payload": {
            "type": "agent_message", "message": "checking things", "phase": "commentary"}},
        {"type": "event_msg", "payload": {
            "type": "agent_message", "message": "here is the answer", "phase": "final_answer"}},
    ]
    msgs, _ = p.parse_transcript_entries(entries, {})
    by_phase = {m.phase: m.text for m in msgs if m.content_type == "text"}
    assert by_phase["commentary"] == "checking things"
    assert by_phase["final_answer"] == "here is the answer"


# ── decoration ─────────────────────────────────────────────────────────────


def test_commentary_gets_marker_final_answer_does_not():
    assert _decorate(_commentary(), "narration").startswith(":speech_balloon:")
    assert _decorate(_final(), "the answer") == "the answer"


# ── store / query / toggle ─────────────────────────────────────────────────


def test_commentary_visibility_defaults_shown():
    assert window_query.is_commentary_hidden("@1") is False


def test_commentary_toggle_and_persist():
    window_store.set_commentary_visibility("@1", "hidden")
    assert window_query.is_commentary_hidden("@1") is True
    assert window_store.toggle_commentary_visibility("@1") == "shown"

    state = WindowState(session_id="s", cwd="/x")
    assert "commentary_visibility" not in state.to_dict()
    state.commentary_visibility = "hidden"
    assert WindowState.from_dict(state.to_dict()).commentary_visibility == "hidden"


def test_set_commentary_visibility_rejects_bad_value():
    with pytest.raises(ValueError):
        window_store.set_commentary_visibility("@1", "sometimes")


# ── suppression when hidden ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_hidden_commentary_is_suppressed(monkeypatch):
    _bind("C1", "@1")
    window_store.set_commentary_visibility("@1", "hidden")

    async def _noop_status(*_a, **_k):
        return None

    monkeypatch.setattr("ccslack.handlers.status.update_status", _noop_status)

    handled = await _pre_post_suppressed(
        FakeSlackClient(), "C1", "@1", _commentary(), "narration"
    )
    assert handled is True


@pytest.mark.asyncio
async def test_shown_commentary_is_not_suppressed(monkeypatch):
    _bind("C1", "@1")  # default shown

    async def _noop_status(*_a, **_k):
        return None

    monkeypatch.setattr("ccslack.handlers.status.update_status", _noop_status)

    handled = await _pre_post_suppressed(
        FakeSlackClient(), "C1", "@1", _commentary(), "narration"
    )
    assert handled is False  # posts normally (with marker)


@pytest.mark.asyncio
async def test_final_answer_posts_even_when_commentary_hidden(monkeypatch):
    _bind("C1", "@1")
    window_store.set_commentary_visibility("@1", "hidden")

    async def _noop_status(*_a, **_k):
        return None

    monkeypatch.setattr("ccslack.handlers.status.update_status", _noop_status)

    handled = await _pre_post_suppressed(
        FakeSlackClient(), "C1", "@1", _final(), "the answer"
    )
    assert handled is False


# ── mute buffer excludes commentary ────────────────────────────────────────


def test_buffer_skips_commentary_keeps_final():
    _buffer_suppressed_answer("@1", _commentary(), "narration")
    assert mute_buffer.take("@1") is None  # commentary not buffered
    _buffer_suppressed_answer("@1", _final(), "the answer")
    assert mute_buffer.take("@1") == "the answer"


# ── command ─────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_commentary_command_hides_and_toggles():
    _bind("C1", "@1")
    client = FakeSlackClient()

    await _handle_commentary(client, "C1", "U1", ["hide"])
    assert window_query.is_commentary_hidden("@1") is True

    await _handle_commentary(client, "C1", "U1", [])  # toggle back
    assert window_query.is_commentary_hidden("@1") is False


@pytest.mark.asyncio
async def test_commentary_command_rejects_unbound():
    client = FakeSlackClient()
    await _handle_commentary(client, "C0NOPE", "U1", ["hide"])
    assert "bound session channel" in client.last_call("chat_postEphemeral").kwargs["text"]


@pytest.mark.asyncio
async def test_commentary_command_rejects_bad_arg():
    _bind("C1", "@1")
    client = FakeSlackClient()
    await _handle_commentary(client, "C1", "U1", ["maybe"])
    assert "unknown mode" in client.last_call("chat_postEphemeral").kwargs["text"]
