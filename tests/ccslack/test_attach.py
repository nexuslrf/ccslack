"""Tests for /ccslack attach — detect and bind the live tmux pane's session."""

import pytest

from ccslack.handlers.meta import _handle_attach
from ccslack.slack_client import FakeSlackClient
from ccslack.thread_router import thread_router


def _bind(channel: str, window: str) -> None:
    thread_router.bind_channel(channel, window, window_name="test")


@pytest.fixture(autouse=True)
def _allow_auth(monkeypatch):
    monkeypatch.setattr("ccslack.handlers.auth.is_authorized", lambda *_a, **_k: True)


@pytest.mark.asyncio
async def test_attach_rejects_unbound_channel(monkeypatch):
    thread_router.reset()
    client = FakeSlackClient()

    await _handle_attach(client, "C0NOBIND", "U0USER")

    text = client.last_call("chat_postEphemeral").kwargs["text"]
    assert "isn't bound" in text
    assert "here" in text


@pytest.mark.asyncio
async def test_attach_rejects_dead_window(monkeypatch):
    thread_router.reset()
    _bind("C0DEAD", "@9")
    client = FakeSlackClient()

    # Stub find_window_by_id to return None (dead window)
    from ccslack import tmux_manager

    async def _dead(_wid):
        return None

    monkeypatch.setattr(tmux_manager.tmux_manager, "find_window_by_id", _dead)

    await _handle_attach(client, "C0DEAD", "U0USER")

    text = client.last_call("chat_postEphemeral").kwargs["text"]
    assert "gone" in text
    assert "restore" in text


@pytest.mark.asyncio
async def test_attach_rejects_no_agent_detected(monkeypatch):
    thread_router.reset()
    _bind("C0SHELL", "@7")
    client = FakeSlackClient()

    from ccslack import tmux_manager
    from ccslack.tmux_manager import TmuxWindow

    async def _live_shell(_wid):
        return TmuxWindow(
            window_id="@7",
            window_name="test",
            cwd="/tmp",
            pane_current_command="vim",
        )

    monkeypatch.setattr(tmux_manager.tmux_manager, "find_window_by_id", _live_shell)

    await _handle_attach(client, "C0SHELL", "U0USER")

    text = client.last_call("chat_postEphemeral").kwargs["text"]
    assert "couldn't detect" in text
    assert "vim" in text


@pytest.mark.asyncio
async def test_attach_binds_to_discovered_session(monkeypatch):
    thread_router.reset()
    _bind("C0ATTACH", "@8")
    client = FakeSlackClient()

    from ccslack import tmux_manager
    from ccslack.providers.base import SessionStartEvent
    from ccslack.tmux_manager import TmuxWindow

    async def _live_claude(_wid):
        return TmuxWindow(
            window_id="@8",
            window_name="test",
            cwd="/home/ruofanl/Projects/test",
            pane_current_command="claude",
        )

    monkeypatch.setattr(tmux_manager.tmux_manager, "find_window_by_id", _live_claude)

    # Stub discover_transcript to return a session

    class _StubProvider:
        class _Caps:
            supports_hookless_discovery = True

        @property
        def capabilities(self):
            return self._Caps()

        def discover_transcript(self, cwd, window_key, **kw):
            return SessionStartEvent(
                session_id="abc123",
                cwd=cwd,
                transcript_path="/tmp/test.jsonl",
                window_key=window_key,
            )

    # Monkeypatch get_provider_for_window to return our stub
    import ccslack.providers as providers_mod

    monkeypatch.setattr(
        providers_mod,
        "get_provider_for_window",
        lambda wid, provider_name=None: _StubProvider(),
    )

    await _handle_attach(client, "C0ATTACH", "U0USER")

    text = client.last_call("chat_postEphemeral").kwargs["text"]
    assert "Attached" in text
    assert "abc123" in text
