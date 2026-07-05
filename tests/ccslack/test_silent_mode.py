import pytest

from ccslack.handlers.messaging_pipeline.message_routing import _should_skip_for_mode
from ccslack.handlers.meta import _handle_mute
from ccslack.monitor_events import NewMessage
from ccslack.slack_client import FakeSlackClient
from ccslack.thread_router import thread_router
from ccslack.window_state_store import NOTIFICATION_MODES, window_store


@pytest.fixture(autouse=True)
def _clean():
    window_store.window_states.clear()
    thread_router.reset()
    yield
    window_store.window_states.clear()
    thread_router.reset()


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
