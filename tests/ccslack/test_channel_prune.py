import pytest
from slack_sdk.errors import SlackApiError

from ccslack.handlers import status
from ccslack.handlers.polling.coordinator import is_channel_gone, prune_channel
from ccslack.session import session_manager
from ccslack.slack_client import FakeSlackClient
from ccslack.thread_router import thread_router
from ccslack.window_state_store import window_store


class _Resp:
    def __init__(self, error: str):
        self.data = {"ok": False, "error": error}

    def get(self, key, default=None):
        return self.data.get(key, default)

    def __getitem__(self, key):
        return self.data[key]


def _err(code: str) -> SlackApiError:
    return SlackApiError(message=code, response=_Resp(code))


def test_is_channel_gone_codes():
    assert is_channel_gone("channel_not_found")
    assert is_channel_gone("is_archived")
    assert not is_channel_gone("message_not_found")
    assert not is_channel_gone("ratelimited")
    assert not is_channel_gone(None)
    assert not is_channel_gone("")


def test_prune_channel_removes_binding_and_window():
    session_manager.set_window_provider("@7", "claude", cwd="/tmp")
    thread_router.bind_channel("C0GONE", "@7", window_name="proj")
    assert thread_router.get_window_for_channel("C0GONE") == "@7"

    prune_channel("C0GONE", "@7")

    assert thread_router.get_window_for_channel("C0GONE") is None
    assert "@7" not in window_store.window_states


def test_prune_channel_resolves_window_when_omitted():
    session_manager.set_window_provider("@8", "codex", cwd="/tmp")
    thread_router.bind_channel("C0GONE2", "@8")
    prune_channel("C0GONE2")  # no window_id — must resolve via binding
    assert thread_router.get_window_for_channel("C0GONE2") is None
    assert "@8" not in window_store.window_states


def test_prune_channel_idempotent():
    # Pruning an unknown channel must not raise.
    prune_channel("C0NEVER", "@nope")


@pytest.mark.asyncio
async def test_ensure_status_message_prunes_on_channel_not_found():
    session_manager.set_window_provider("@9", "claude", cwd="/tmp")
    thread_router.bind_channel("C0GONE3", "@9", window_name="proj")

    client = FakeSlackClient()
    client.set_side_effect("chat_postMessage", [_err("channel_not_found")])

    ts = await status.ensure_status_message(client, "C0GONE3", "@9")
    assert ts is None
    # Binding pruned so the poll loop stops retrying.
    assert thread_router.get_window_for_channel("C0GONE3") is None


@pytest.mark.asyncio
async def test_ensure_status_message_keeps_binding_on_transient_error():
    session_manager.set_window_provider("@10", "claude", cwd="/tmp")
    thread_router.bind_channel("C0OK", "@10", window_name="proj")

    client = FakeSlackClient()
    client.set_side_effect("chat_postMessage", [_err("ratelimited")])

    ts = await status.ensure_status_message(client, "C0OK", "@10")
    assert ts is None
    # Transient error must NOT prune the binding.
    assert thread_router.get_window_for_channel("C0OK") == "@10"
