import pytest

from ccslack.config import config
from ccslack.handlers import meta
from ccslack.handlers.auth import is_authorized
from ccslack.handlers.meta import (
    _create_unique_channel,
    _handle_grant,
    _handle_here,
    _parse_user_ids,
)
from ccslack.slack_client import FakeSlackClient
from ccslack.thread_router import thread_router


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    thread_router.reset()
    monkeypatch.setattr(config, "allowed_users", {"UADMIN"})
    yield
    thread_router.reset()


# --- auth composition ------------------------------------------------------


def test_allowed_user_always_authorized():
    assert is_authorized("UADMIN", "C1") is True
    assert is_authorized("UADMIN", "") is True


def test_private_mode_trusts_channel_members(monkeypatch):
    monkeypatch.setattr(config, "public_channels", False)
    thread_router.bind_channel("C1", "@1")
    # A non-allowed user is trusted purely by membership in a bound channel.
    assert is_authorized("URANDOM", "C1") is True


def test_public_mode_does_not_trust_members(monkeypatch):
    monkeypatch.setattr(config, "public_channels", True)
    thread_router.bind_channel("C1", "@1")
    assert is_authorized("URANDOM", "C1") is False  # membership no longer enough


def test_public_mode_honors_per_channel_grant(monkeypatch):
    monkeypatch.setattr(config, "public_channels", True)
    thread_router.bind_channel("C1", "@1")
    thread_router.grant_user("C1", "UGRANTED")
    assert is_authorized("UGRANTED", "C1") is True
    assert is_authorized("UGRANTED", "C2") is False  # grant is per-channel


# --- user-id parsing -------------------------------------------------------


def test_parse_user_ids_mentions_and_bare():
    assert _parse_user_ids(["<@U123ABC|alice>"]) == ["U123ABC"]
    assert _parse_user_ids(["<@U123ABC>"]) == ["U123ABC"]
    assert _parse_user_ids(["U123ABC"]) == ["U123ABC"]
    assert _parse_user_ids(["W123ABC"]) == ["W123ABC"]
    assert _parse_user_ids(["notauser"]) == []
    # de-dups, preserves order
    assert _parse_user_ids(["U1ABCDE", "<@U1ABCDE>", "U2ABCDE"]) == ["U1ABCDE", "U2ABCDE"]


# --- /ccslack adduser ------------------------------------------------------


@pytest.mark.asyncio
async def test_adduser_grants_when_admin(monkeypatch):
    thread_router.bind_channel("C1", "@1")
    client = FakeSlackClient()

    await _handle_grant(client, "C1", "UADMIN", ["<@UNEW|bob>"], grant=True)

    assert thread_router.is_user_granted("C1", "UNEW") is True
    assert client.call_count("chat_postMessage") == 1


@pytest.mark.asyncio
async def test_adduser_rejected_for_non_admin(monkeypatch):
    thread_router.bind_channel("C1", "@1")
    client = FakeSlackClient()

    await _handle_grant(client, "C1", "URANDOM", ["<@UNEW>"], grant=True)

    assert thread_router.is_user_granted("C1", "UNEW") is False
    eph = client.last_call("chat_postEphemeral")
    assert "ALLOWED_USERS" in eph.kwargs["text"]


@pytest.mark.asyncio
async def test_adduser_rejected_in_unbound_channel():
    client = FakeSlackClient()
    await _handle_grant(client, "C0", "UADMIN", ["<@UNEW>"], grant=True)
    eph = client.last_call("chat_postEphemeral")
    assert "bound session channel" in eph.kwargs["text"]


@pytest.mark.asyncio
async def test_removeuser_revokes(monkeypatch):
    thread_router.bind_channel("C1", "@1")
    thread_router.grant_user("C1", "UNEW")
    client = FakeSlackClient()

    await _handle_grant(client, "C1", "UADMIN", ["<@UNEW>"], grant=False)

    assert thread_router.is_user_granted("C1", "UNEW") is False


# --- public channel creation ----------------------------------------------


@pytest.mark.asyncio
async def test_create_channel_public_in_public_mode(monkeypatch):
    monkeypatch.setattr(config, "public_channels", True)
    client = FakeSlackClient()
    client.returns["conversations_create"] = {"ok": True, "channel": {"id": "CNEW"}}

    channel_id, err = await _create_unique_channel(client, "ccslack-x", "@9")

    assert channel_id == "CNEW"
    assert err == ""
    call = client.last_call("conversations_create")
    assert call.kwargs["is_private"] is False


@pytest.mark.asyncio
async def test_create_channel_private_by_default(monkeypatch):
    monkeypatch.setattr(config, "public_channels", False)
    client = FakeSlackClient()
    client.returns["conversations_create"] = {"ok": True, "channel": {"id": "CNEW"}}

    await _create_unique_channel(client, "ccslack-x", "@9")

    call = client.last_call("conversations_create")
    assert call.kwargs["is_private"] is True


# --- /ccslack here (bring-your-own-channel) --------------------------------


@pytest.mark.asyncio
async def test_here_binds_current_channel(monkeypatch, tmp_path):
    spawned = {}

    async def _fake_restore(c, ch, *, provider, cwd, **kw):
        spawned["channel"] = ch
        spawned["cwd"] = cwd
        spawned["provider"] = provider
        return "@77"

    monkeypatch.setattr(meta, "_SUPPORTED_PROVIDERS", {"claude", "codex", "shell"})
    monkeypatch.setattr(
        "ccslack.handlers.recovery.restore_in_channel", _fake_restore
    )
    client = FakeSlackClient()

    await _handle_here(client, "CHUMAN", "UADMIN", [str(tmp_path), "codex"])

    assert spawned == {
        "channel": "CHUMAN",
        "cwd": str(tmp_path.resolve()),
        "provider": "codex",
    }


@pytest.mark.asyncio
async def test_here_rejects_already_bound_channel(tmp_path):
    thread_router.bind_channel("CBOUND", "@1")
    client = FakeSlackClient()

    await _handle_here(client, "CBOUND", "UADMIN", [str(tmp_path)])

    eph = client.last_call("chat_postEphemeral")
    assert "already a session" in eph.kwargs["text"]
