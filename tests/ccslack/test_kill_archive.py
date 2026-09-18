"""Tests for /ccslack kill archive flag behaviour."""

import pytest

from ccslack.config import config
from ccslack.handlers.meta import _handle_kill, _kill_one
from ccslack.slack_client import FakeSlackClient


def _bind(channel: str, window: str) -> None:
    from ccslack.thread_router import thread_router

    thread_router.bind_channel(channel, window, window_name="test")


def _stub_kill_deps(monkeypatch) -> None:
    from ccslack import tmux_manager

    async def _noop_kill(*a, **kw):
        return None

    monkeypatch.setattr(tmux_manager.tmux_manager, "kill_window", _noop_kill)


@pytest.mark.asyncio
async def test_kill_default_does_not_archive(monkeypatch):
    _stub_kill_deps(monkeypatch)
    _bind("C0KILL", "@9")
    client = FakeSlackClient()

    await _kill_one(client, "C0KILL", "@9")

    # No conversations_archive call when archive=False (the new default).
    assert client.call_count("conversations_archive") == 0


@pytest.mark.asyncio
async def test_kill_with_archive_flag_archives(monkeypatch):
    _stub_kill_deps(monkeypatch)
    _bind("C0KILL", "@9")
    client = FakeSlackClient()

    await _kill_one(client, "C0KILL", "@9", archive=True)

    assert client.call_count("conversations_archive") == 1
    call = client.last_call("conversations_archive")
    assert call.kwargs["channel"] == "C0KILL"


@pytest.mark.asyncio
async def test_kill_default_reports_unbound_hint(monkeypatch):
    _stub_kill_deps(monkeypatch)
    _bind("C0KILL", "@9")
    client = FakeSlackClient()

    result = await _kill_one(client, "C0KILL", "@9")

    assert "unbound" in result
    assert "here" in result


@pytest.mark.asyncio
async def test_kill_archive_reports_archived(monkeypatch):
    _stub_kill_deps(monkeypatch)
    _bind("C0KILL", "@9")
    client = FakeSlackClient()

    result = await _kill_one(client, "C0KILL", "@9", archive=True)

    assert "archived" in result


@pytest.mark.asyncio
async def test_handle_kill_strips_archive_flag(monkeypatch):
    """--archive is parsed out and not treated as a target argument."""
    monkeypatch.setattr(config, "meta_channel_id", "C0META")
    monkeypatch.setattr(config, "meta_surface", "channel")
    _stub_kill_deps(monkeypatch)
    _bind("C0KILL", "@9")
    client = FakeSlackClient()

    # From the meta channel, target C0KILL with --archive.
    await _handle_kill(client, "C0META", "U0ALLOWED", ["C0KILL", "--archive"])

    # Should have archived exactly once (the flag was consumed, not treated
    # as part of the target).
    assert client.call_count("conversations_archive") == 1


def test_safe_archive_renamed_prefixes_channel(monkeypatch):
    """safe_archive_renamed renames to archive-<name> before archiving."""
    from ccslack.slack_client import FakeSlackClient
    from ccslack.slack_sender import safe_archive_renamed

    client = FakeSlackClient()
    client.returns["conversations_info"] = {
        "ok": True,
        "channel": {"id": "C1", "name": "my-session"},
    }

    import asyncio

    ok = asyncio.run(safe_archive_renamed(client, channel="C1"))
    assert ok is True
    rename = client.last_call("conversations_rename")
    assert rename.kwargs["name"] == "archive-my-session"
    assert client.call_count("conversations_archive") == 1
    # rename happens before archive
    calls = [c.method for c in client.calls]
    assert calls.index("conversations_rename") < calls.index("conversations_archive")


def test_safe_archive_renamed_skips_double_prefix():
    from ccslack.slack_client import FakeSlackClient
    from ccslack.slack_sender import safe_archive_renamed

    client = FakeSlackClient()
    client.returns["conversations_info"] = {
        "ok": True,
        "channel": {"id": "C1", "name": "archive-my-session"},
    }

    import asyncio

    asyncio.run(safe_archive_renamed(client, channel="C1"))
    assert client.call_count("conversations_rename") == 0  # already prefixed
    assert client.call_count("conversations_archive") == 1


def test_safe_archive_renamed_rename_failure_still_archives():
    from slack_sdk.errors import SlackApiError

    from ccslack.slack_client import FakeSlackClient
    from ccslack.slack_sender import safe_archive_renamed

    client = FakeSlackClient()
    client.returns["conversations_info"] = {
        "ok": True,
        "channel": {"id": "C1", "name": "my-session"},
    }

    def _make_err(msg):
        resp = type("R", (), {"get": lambda self, k: msg})()
        return SlackApiError(msg, resp)

    client.set_side_effect("conversations_rename", [_make_err("invalid_name")])

    import asyncio

    ok = asyncio.run(safe_archive_renamed(client, channel="C1"))
    assert ok is True  # archive succeeded despite the rename failure
