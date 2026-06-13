import pytest
from slack_sdk.errors import SlackApiError

from ccslack.config import config
from ccslack.handlers.meta import _do_join, _post_join_offer
from ccslack.slack_client import FakeSlackClient


@pytest.mark.asyncio
async def test_join_offer_posts_with_others_mentioned(monkeypatch):
    monkeypatch.setattr(config, "join_offer", True)
    monkeypatch.setattr(config, "meta_channel_id", "CMETA")
    monkeypatch.setattr(config, "allowed_users", {"UCREATOR", "UALICE", "UBOB"})
    client = FakeSlackClient()

    await _post_join_offer(client, "CNEW", "UCREATOR", "codex", "/proj")

    msg = client.last_call("chat_postMessage")
    assert msg is not None
    assert msg.kwargs["channel"] == "CMETA"
    blob = str(msg.kwargs["blocks"])
    assert "ccslack_join_session" in blob
    assert "CNEW" in blob
    assert "<@UALICE>" in blob and "<@UBOB>" in blob


def _post_join_offer_blob(client) -> str:
    return str(client.last_call("chat_postMessage").kwargs["blocks"])


@pytest.mark.asyncio
async def test_join_offer_excludes_creator_from_invite_list(monkeypatch):
    monkeypatch.setattr(config, "join_offer", True)
    monkeypatch.setattr(config, "meta_channel_id", "CMETA")
    monkeypatch.setattr(config, "allowed_users", {"UCREATOR", "UALICE"})
    client = FakeSlackClient()

    await _post_join_offer(client, "CNEW", "UCREATOR", "codex", "/proj")

    blob = _post_join_offer_blob(client)
    # Creator appears once (as the starter), never in the "want to join" list.
    assert blob.count("<@UCREATOR>") == 1
    assert "<@UALICE>" in blob


@pytest.mark.asyncio
async def test_join_offer_skips_when_no_other_users(monkeypatch):
    monkeypatch.setattr(config, "join_offer", True)
    monkeypatch.setattr(config, "meta_channel_id", "CMETA")
    monkeypatch.setattr(config, "allowed_users", {"UCREATOR"})
    client = FakeSlackClient()

    await _post_join_offer(client, "CNEW", "UCREATOR", "codex", "/proj")

    assert client.call_count("chat_postMessage") == 0


@pytest.mark.asyncio
async def test_join_offer_skips_when_disabled(monkeypatch):
    monkeypatch.setattr(config, "join_offer", False)
    monkeypatch.setattr(config, "allowed_users", {"UCREATOR", "UALICE"})
    client = FakeSlackClient()

    await _post_join_offer(client, "CNEW", "UCREATOR", "codex", "/proj")

    assert client.call_count("chat_postMessage") == 0


@pytest.mark.asyncio
async def test_do_join_invites_and_confirms(monkeypatch):
    monkeypatch.setattr(config, "meta_channel_id", "CMETA")
    client = FakeSlackClient()

    await _do_join(client, "UALICE", "CNEW")

    inv = client.last_call("conversations_invite")
    assert inv is not None
    assert inv.kwargs["channel"] == "CNEW"
    assert inv.kwargs["users"] == "UALICE"
    eph = client.last_call("chat_postEphemeral")
    assert "Added you" in eph.kwargs["text"]


@pytest.mark.asyncio
async def test_do_join_already_member_is_graceful(monkeypatch):
    monkeypatch.setattr(config, "meta_channel_id", "CMETA")
    client = FakeSlackClient()
    client.set_side_effect(
        "conversations_invite",
        [SlackApiError("x", {"error": "already_in_channel"})],
    )

    await _do_join(client, "UALICE", "CNEW")

    eph = client.last_call("chat_postEphemeral")
    assert "already in" in eph.kwargs["text"]
