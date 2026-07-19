import pytest

from ccslack.handlers import mute_buffer
from ccslack.handlers.messaging_pipeline.message_routing import (
    _pre_post_suppressed,
    _should_skip_for_mode,
)
from ccslack.handlers.meta import _handle_mute
from ccslack.monitor_events import NewMessage
from ccslack.session import session_manager
from ccslack.slack_client import FakeSlackClient
from ccslack.thread_router import thread_router
from ccslack.window_state_store import NOTIFICATION_MODES, window_store


@pytest.fixture(autouse=True)
def _clean():
    window_store.window_states.clear()
    thread_router.reset()
    mute_buffer.reset()
    yield
    window_store.window_states.clear()
    thread_router.reset()
    mute_buffer.reset()


def _msg(content_type: str) -> NewMessage:
    return NewMessage(
        session_id="s1",
        text="something happened",
        is_complete=True,
        content_type=content_type,
    )


def test_silent_is_a_registered_mode():
    assert "silent" in NOTIFICATION_MODES


def test_silent_skips_text():
    assert _should_skip_for_mode("silent", "hello", _msg("text")) is True


def test_silent_skips_tool_flows():
    assert _should_skip_for_mode("silent", "$ ls", _msg("tool_use")) is True
    assert _should_skip_for_mode("silent", "out", _msg("tool_result")) is True


def test_muted_still_posts_tool_flows_but_silent_does_not():
    # Contrast: muted keeps tool flows so the agent can progress.
    assert _should_skip_for_mode("muted", "out", _msg("tool_result")) is False
    assert _should_skip_for_mode("silent", "out", _msg("tool_result")) is True


def test_all_mode_never_skips():
    assert _should_skip_for_mode("all", "x", _msg("text")) is False


def test_cycle_reaches_silent_and_wraps():
    window_store.set_notification_mode("@1", "muted")
    assert window_store.cycle_notification_mode("@1") == "silent"
    assert window_store.cycle_notification_mode("@1") == "all"


def test_set_notification_mode_accepts_silent():
    window_store.set_notification_mode("@2", "silent")
    assert window_store.get_notification_mode("@2") == "silent"


@pytest.mark.asyncio
async def test_mute_command_recognizes_silent_alias():
    thread_router.bind_channel("C1", "@7", window_name="dev")
    client = FakeSlackClient()

    await _handle_mute(client, "C1", "U1", ["quiet"])

    assert window_store.get_notification_mode("@7") == "silent"
    text = client.last_call("chat_postEphemeral").kwargs["text"]
    assert "silent" in text


@pytest.mark.asyncio
async def test_mute_command_rejects_unknown_mode():
    thread_router.bind_channel("C1", "@7", window_name="dev")
    client = FakeSlackClient()

    await _handle_mute(client, "C1", "U1", ["bogus"])

    text = client.last_call("chat_postEphemeral").kwargs["text"]
    assert "silent" in text  # usage hint now lists silent


# ── suppressed-answer buffer + flush on un-mute ────────────────────────────


def _bind(channel: str, window: str) -> None:
    session_manager.set_window_provider(window, "claude", cwd="/x")
    thread_router.bind_channel(channel, window, window_name="p")


def _answer(text: str) -> NewMessage:
    return NewMessage(
        session_id="s1", text=text, is_complete=True, content_type="text"
    )


@pytest.mark.asyncio
async def test_silent_buffers_the_suppressed_answer(monkeypatch):
    _bind("C1", "@1")
    window_store.set_notification_mode("@1", "silent")

    async def _noop_status(*_a, **_k):
        return None

    monkeypatch.setattr("ccslack.handlers.status.update_status", _noop_status)

    handled = await _pre_post_suppressed(
        FakeSlackClient(), "C1", "@1", _answer("the answer"), "the answer"
    )
    assert handled is True
    assert mute_buffer.take("@1") == "the answer"


@pytest.mark.asyncio
async def test_unmute_flushes_the_buffered_answer():
    _bind("C1", "@1")
    window_store.set_notification_mode("@1", "silent")
    mute_buffer.remember("@1", "missed answer")
    client = FakeSlackClient()

    await _handle_mute(client, "C1", "U1", ["all"])

    posts = [c.kwargs.get("text", "") for c in client.calls if c.method == "chat_postMessage"]
    assert any("missed answer" in p for p in posts)
    assert mute_buffer.take("@1") is None  # consumed


@pytest.mark.asyncio
async def test_going_quieter_does_not_flush():
    _bind("C1", "@1")
    window_store.set_notification_mode("@1", "all")
    mute_buffer.remember("@1", "should stay buffered")
    client = FakeSlackClient()

    await _handle_mute(client, "C1", "U1", ["silent"])  # all → silent (quieter)

    posts = [c for c in client.calls if c.method == "chat_postMessage"]
    assert posts == []
    assert mute_buffer.take("@1") == "should stay buffered"


@pytest.mark.asyncio
async def test_silent_does_not_suppress_interactive_picker(monkeypatch):
    _bind("C1", "@1")
    window_store.set_notification_mode("@1", "silent")
    entered = {}

    async def _fake_enter(_client, *, channel_id, window_id, tool_use_id, tool_name):
        entered.update(window_id=window_id, tool_name=tool_name)

    async def _noop_status(*_a, **_k):
        return None

    monkeypatch.setattr(
        "ccslack.handlers.interactive.enter_interactive_mode", _fake_enter
    )
    monkeypatch.setattr("ccslack.handlers.status.update_status", _noop_status)

    msg = NewMessage(
        session_id="s1",
        text="",
        is_complete=True,
        content_type="tool_use",
        tool_use_id="t1",
        tool_name="AskUserQuestion",
    )
    handled = await _pre_post_suppressed(FakeSlackClient(), "C1", "@1", msg, "")

    assert handled is True
    assert entered == {"window_id": "@1", "tool_name": "AskUserQuestion"}
    # the picker is not a chatter answer — nothing buffered
    assert mute_buffer.take("@1") is None


def test_mute_buffer_take_is_one_shot():
    mute_buffer.remember("@1", "x")
    assert mute_buffer.take("@1") == "x"
    assert mute_buffer.take("@1") is None
