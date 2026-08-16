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
