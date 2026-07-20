import pytest
from slack_sdk.errors import SlackApiError

from ccslack.config import config
from ccslack.handlers.meta import _handle_revive, _resolve_channel_ref
from ccslack.slack_client import FakeSlackClient
from ccslack.thread_router import thread_router
from ccslack.window_state_store import window_store


@pytest.fixture(autouse=True)
def _clean():
    thread_router.reset()
    window_store.window_states.clear()
    yield
    thread_router.reset()
    window_store.window_states.clear()


@pytest.fixture
def _stub_restore(monkeypatch):
    """Stub the recovery core so no real tmux/topic API is needed."""
    calls: dict = {}

    async def _fake_recover(_client, channel):
        calls["recover"] = channel
        return ("codex", "/proj")

    async def _fake_restore(_client, channel, **kwargs):
        calls["restore"] = {"channel": channel, **kwargs}
        return "@42"

    monkeypatch.setattr(
        "ccslack.handlers.recovery.recover_channel_context", _fake_recover
    )
    monkeypatch.setattr(
        "ccslack.handlers.recovery.restore_in_channel", _fake_restore
    )
    return calls


def _api_error(code: str) -> SlackApiError:
    class _Resp(dict):
        def get(self, k, d=None):
            return super().get(k, d)

    return SlackApiError(code, _Resp(error=code))


@pytest.mark.asyncio
async def test_revive_unarchives_recovers_and_restores(_stub_restore):
    client = FakeSlackClient()

    await _handle_revive(client, "C0META", "U1", ["<#C0DEAD>", "resume"])

    ua = client.last_call("conversations_unarchive")
    assert ua is not None and ua.kwargs["channel"] == "C0DEAD"
    assert _stub_restore["recover"] == "C0DEAD"
    assert _stub_restore["restore"]["channel"] == "C0DEAD"
    assert _stub_restore["restore"]["mode"] == "resume"
    assert _stub_restore["restore"]["provider"] == "codex"
    assert _stub_restore["restore"]["cwd"] == "/proj"
    assert "revived <#C0DEAD>" in client.last_call("chat_postEphemeral").kwargs["text"]


@pytest.mark.asyncio
async def test_revive_defaults_to_continue(_stub_restore):
    client = FakeSlackClient()
    await _handle_revive(client, "C0META", "U1", ["<#C0DEAD>"])
    assert _stub_restore["restore"]["mode"] == "continue"


@pytest.mark.asyncio
async def test_revive_requires_channel_ref():
    client = FakeSlackClient()
    await _handle_revive(client, "C0META", "U1", ["resume"])
    assert client.call_count("conversations_unarchive") == 0
    assert "usage" in client.last_call("chat_postEphemeral").kwargs["text"]


@pytest.mark.asyncio
async def test_revive_accepts_bare_channel_id(_stub_restore):
    client = FakeSlackClient()
    await _handle_revive(client, "C0META", "U1", ["C0DEAD1", "resume"])
    assert client.last_call("conversations_unarchive").kwargs["channel"] == "C0DEAD1"
    assert _stub_restore["restore"]["channel"] == "C0DEAD1"


@pytest.mark.asyncio
async def test_revive_resolves_archived_channel_by_name(monkeypatch, _stub_restore):
    monkeypatch.setattr(config, "public_channels", False)
    client = FakeSlackClient()
    client.returns["conversations_list"] = {
        "ok": True,
        "channels": [
            {"id": "C0OTHER", "name": "random"},
            {"id": "C0KILLED", "name": "ccslack-myproj"},
        ],
        "response_metadata": {"next_cursor": ""},
    }

    await _handle_revive(client, "C0META", "U1", ["#ccslack-myproj", "continue"])

    assert client.last_call("conversations_unarchive").kwargs["channel"] == "C0KILLED"
    assert _stub_restore["restore"]["channel"] == "C0KILLED"


@pytest.mark.asyncio
async def test_revive_unknown_name_reports_id_hint():
    client = FakeSlackClient()
    client.returns["conversations_list"] = {
        "ok": True,
        "channels": [{"id": "C0OTHER", "name": "random"}],
        "response_metadata": {"next_cursor": ""},
    }

    await _handle_revive(client, "C0META", "U1", ["nope-channel"])

    assert client.call_count("conversations_unarchive") == 0
    text = client.last_call("chat_postEphemeral").kwargs["text"]
    assert "couldn't find" in text and "archives/C" in text


@pytest.mark.asyncio
async def test_resolve_channel_ref_forms(monkeypatch):
    client = FakeSlackClient()
    # mention + bare id resolve without any API call
    assert await _resolve_channel_ref(client, "<#C0ABCDEF|name>") == "C0ABCDEF"
    assert await _resolve_channel_ref(client, "C0ABCDEF") == "C0ABCDEF"
    assert client.call_count("conversations_list") == 0


@pytest.mark.asyncio
async def test_revive_reports_unarchive_policy_error(monkeypatch, _stub_restore):
    client = FakeSlackClient()

    async def _boom(*, channel, **kwargs):  # noqa: ARG001
        raise _api_error("restricted_action")

    monkeypatch.setattr(client, "conversations_unarchive", _boom)

    await _handle_revive(client, "C0META", "U1", ["<#C0DEAD>"])

    assert "recover" not in _stub_restore  # bailed before restore
    text = client.last_call("chat_postEphemeral").kwargs["text"]
    assert "restricted_action" in text
    assert "new" in text  # points at the fallback


@pytest.mark.asyncio
async def test_revive_treats_not_archived_as_already_active(monkeypatch, _stub_restore):
    client = FakeSlackClient()

    async def _not_archived(*, channel, **kwargs):  # noqa: ARG001
        raise _api_error("not_archived")

    monkeypatch.setattr(client, "conversations_unarchive", _not_archived)

    await _handle_revive(client, "C0META", "U1", ["<#C0LIVE>", "continue"])

    # Still proceeds to restore.
    assert _stub_restore["restore"]["channel"] == "C0LIVE"
    assert "already active" in client.last_call("chat_postEphemeral").kwargs["text"]


@pytest.mark.asyncio
async def test_revive_reports_non_session_topic(monkeypatch):
    client = FakeSlackClient()

    async def _no_context(_client, channel):  # noqa: ARG001
        return None

    monkeypatch.setattr(
        "ccslack.handlers.recovery.recover_channel_context", _no_context
    )

    await _handle_revive(client, "C0META", "U1", ["<#C0DEAD>"])

    # Un-archived, but couldn't auto-respawn.
    assert client.call_count("conversations_unarchive") == 1
    assert "here" in client.last_call("chat_postEphemeral").kwargs["text"]
