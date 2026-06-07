import pytest
from slack_sdk.errors import SlackApiError

from ccslack.handlers.meta import _handle_rename
from ccslack.session import session_manager
from ccslack.slack_client import FakeSlackClient
from ccslack.thread_router import thread_router


class _Resp:
    def __init__(self, error: str):
        self.data = {"ok": False, "error": error}

    def get(self, key, default=None):
        return self.data.get(key, default)

    def __getitem__(self, key):
        return self.data[key]


def _err(code: str) -> SlackApiError:
    return SlackApiError(message=code, response=_Resp(code))


def _bind(channel_id: str, window_id: str) -> None:
    session_manager.set_window_provider(window_id, "claude", cwd="/tmp")
    thread_router.bind_channel(channel_id, window_id, window_name="proj")


@pytest.mark.asyncio
async def test_rename_calls_conversations_rename_with_sanitized_slug():
    _bind("C0R1", "@40")
    client = FakeSlackClient()

    await _handle_rename(client, "C0R1", "U1", ["My Cool", "Session"])

    call = client.last_call("conversations_rename")
    assert call is not None
    assert call.kwargs["channel"] == "C0R1"
    assert call.kwargs["name"] == "my-cool-session"


@pytest.mark.asyncio
async def test_rename_confirms_to_user():
    _bind("C0R2", "@41")
    client = FakeSlackClient()

    await _handle_rename(client, "C0R2", "U1", ["auth-work"])

    eph = client.last_call("chat_postEphemeral")
    assert "auth-work" in eph.kwargs["text"]


@pytest.mark.asyncio
async def test_rename_rejected_outside_bound_channel():
    client = FakeSlackClient()

    await _handle_rename(client, "C0UNBOUND", "U1", ["whatever"])

    assert client.call_count("conversations_rename") == 0
    eph = client.last_call("chat_postEphemeral")
    assert "bound session channel" in eph.kwargs["text"]


@pytest.mark.asyncio
async def test_rename_requires_a_name():
    _bind("C0R3", "@42")
    client = FakeSlackClient()

    await _handle_rename(client, "C0R3", "U1", [])

    assert client.call_count("conversations_rename") == 0
    eph = client.last_call("chat_postEphemeral")
    assert "usage" in eph.kwargs["text"].lower()


@pytest.mark.asyncio
async def test_rename_reports_name_taken():
    _bind("C0R4", "@43")
    client = FakeSlackClient()
    client.set_side_effect("conversations_rename", [_err("name_taken")])

    await _handle_rename(client, "C0R4", "U1", ["dup"])

    eph = client.last_call("chat_postEphemeral")
    assert "already exists" in eph.kwargs["text"]


@pytest.mark.asyncio
async def test_rename_reports_generic_error():
    _bind("C0R5", "@44")
    client = FakeSlackClient()
    client.set_side_effect("conversations_rename", [_err("ratelimited")])

    await _handle_rename(client, "C0R5", "U1", ["foo"])

    eph = client.last_call("chat_postEphemeral")
    assert "ratelimited" in eph.kwargs["text"]
