import pytest

from ccslack.handlers.new_modal import (
    _build_new_text,
    _forward_new,
    build_new_session_view,
)


def _checkbox_values(view: dict) -> set[str]:
    values: set[str] = set()
    for block in view["blocks"]:
        element = block.get("element", {})
        if element.get("type") != "checkboxes":
            continue
        for option in element.get("options", []):
            values.add(option.get("value"))
    return values


def test_modal_offers_worktree_and_yolo():
    view = build_new_session_view(default_provider="claude", private_metadata="C0META")
    assert _checkbox_values(view) == {"worktree", "yolo"}


def test_modal_defaults_unknown_provider_to_claude():
    view = build_new_session_view(default_provider="bogus", private_metadata="C0META")
    provider_block = next(
        b for b in view["blocks"] if b.get("block_id") == "provider_block"
    )
    initial = provider_block["element"]["initial_option"]["value"]
    assert initial == "claude"


def _host_block(view: dict) -> dict | None:
    return next((b for b in view["blocks"] if b.get("block_id") == "host_block"), None)


def test_modal_host_block_only_for_multi_host():
    one = build_new_session_view(
        default_provider="claude", private_metadata="C0", hosts=["r0"]
    )
    assert _host_block(one) is None  # single host: no selector

    many = build_new_session_view(
        default_provider="claude",
        private_metadata="C0",
        hosts=["r0", "gpu1", "gpu2"],
        default_host="r0",
    )
    block = _host_block(many)
    assert block is not None
    values = [o["value"] for o in block["element"]["options"]]
    assert values == ["r0", "gpu1", "gpu2"]
    assert block["element"]["initial_option"]["value"] == "r0"


def test_build_new_text_roundtrips_flags():
    text = _build_new_text(
        directory="/p ath",
        provider="codex",
        want_worktree=True,
        branch="feat-x",
        want_yolo=True,
        host="gpu1",
    )
    assert text == "new '/p ath' codex --worktree feat-x --yolo --host gpu1"


@pytest.mark.asyncio
async def test_forward_new_sends_synthetic_command(monkeypatch):
    from ccslack import fleet_state
    from ccslack.config import config
    from ccslack.slack_client import FakeSlackClient

    monkeypatch.setattr(config, "slash_command", "/ccslack")
    sent: list = []

    async def _fake_forward(host, payload):
        sent.append((host, payload))
        return True

    monkeypatch.setattr(fleet_state, "forward", _fake_forward)
    client = FakeSlackClient()

    await _forward_new(
        client,
        meta_channel="C0META",
        user_id="U1",
        directory="/proj",
        provider="codex",
        want_worktree=False,
        branch=None,
        want_yolo=False,
        host="gpu1",
    )

    assert len(sent) == 1
    host, payload = sent[0]
    assert host == "gpu1"
    assert payload["command"] == "/ccslack"
    assert payload["text"] == "new /proj codex --host gpu1"
    assert payload["channel_id"] == "C0META"
    assert payload["user_id"] == "U1"


@pytest.mark.asyncio
async def test_forward_new_reports_unreachable_host(monkeypatch):
    from ccslack import fleet_state
    from ccslack.slack_client import FakeSlackClient

    async def _fail(host, payload):
        return False

    monkeypatch.setattr(fleet_state, "forward", _fail)
    client = FakeSlackClient()

    await _forward_new(
        client,
        meta_channel="C0META",
        user_id="U1",
        directory="/proj",
        provider="codex",
        want_worktree=False,
        branch=None,
        want_yolo=False,
        host="ghost",
    )

    eph = client.last_call("chat_postEphemeral")
    assert "couldn't reach host `ghost`" in eph.kwargs["text"]
