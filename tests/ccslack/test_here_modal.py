"""Tests for the /ccslack here modal (bind existing channel)."""

import pytest

from ccslack.handlers.new_modal import build_new_session_view, open_here_modal


def test_here_modal_uses_here_callback_id():
    view = build_new_session_view(default_provider="pi", private_metadata="C1")
    view["callback_id"] = "ccslack_here_modal"
    view["title"]["text"] = "Bind this channel"
    view["submit"]["text"] = "Bind"
    assert view["callback_id"] == "ccslack_here_modal"
    assert view["title"]["text"] == "Bind this channel"
    assert view["submit"]["text"] == "Bind"


def test_here_modal_has_no_host_selector():
    """here always binds locally — no host selector block."""
    view = build_new_session_view(
        default_provider="claude", private_metadata="C1", hosts=None, default_host=""
    )
    block_ids = [b.get("block_id") for b in view["blocks"]]
    assert "host_block" not in block_ids


def test_here_modal_preserves_directory_and_provider_inputs():
    view = build_new_session_view(default_provider="codex", private_metadata="C1")
    block_ids = [b.get("block_id") for b in view["blocks"]]
    assert "directory_block" in block_ids
    assert "provider_block" in block_ids


@pytest.mark.asyncio
async def test_open_here_modal_calls_views_open(monkeypatch):
    captured = {}

    class _FakeClient:
        async def views_open(self, **kw):
            captured.update(kw)
            return {"ok": True}

    await open_here_modal(_FakeClient(), trigger_id="T123", channel_id="C456")
    assert captured["trigger_id"] == "T123"
    view = captured["view"]
    assert view["callback_id"] == "ccslack_here_modal"
    assert view["private_metadata"] == "C456"
    assert view["title"]["text"] == "Bind this channel"
    assert view["submit"]["text"] == "Bind"
