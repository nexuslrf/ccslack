import pytest

from ccslack.handlers.meta import _handle_screenshot, _handle_toolbar
from ccslack.session import session_manager
from ccslack.slack_client import FakeSlackClient
from ccslack.thread_router import thread_router
from ccslack.window_state_store import window_store


@pytest.fixture(autouse=True)
def _clean():
    window_store.window_states.clear()
    thread_router.reset()
    yield
    window_store.window_states.clear()
    thread_router.reset()


def _bind(channel: str, window: str) -> None:
    session_manager.set_window_provider(window, "claude", cwd="/x")
    thread_router.bind_channel(channel, window, window_name="p")


@pytest.mark.asyncio
async def test_screenshot_shortcut_captures_bound_window(monkeypatch):
    _bind("C1", "@7")
    seen = {}

    async def _fake_upload(client, *, channel_id, window_id, user_id):
        seen.update(channel_id=channel_id, window_id=window_id, user_id=user_id)
        return True

    monkeypatch.setattr("ccslack.handlers.screenshot.upload_screenshot", _fake_upload)
    await _handle_screenshot(FakeSlackClient(), "C1", "U1")
    assert seen == {"channel_id": "C1", "window_id": "@7", "user_id": "U1"}


@pytest.mark.asyncio
async def test_screenshot_shortcut_rejects_unbound_channel(monkeypatch):
    called = False

    async def _fake_upload(*_a, **_k):
        nonlocal called
        called = True

    monkeypatch.setattr("ccslack.handlers.screenshot.upload_screenshot", _fake_upload)
    client = FakeSlackClient()
    await _handle_screenshot(client, "C_UNBOUND", "U1")
    assert not called
    assert "session channel" in client.last_call("chat_postEphemeral").kwargs["text"]


@pytest.mark.asyncio
async def test_toolbar_shortcut_opens_for_bound_window(monkeypatch):
    _bind("C1", "@7")
    seen = {}

    async def _fake_open(client, channel_id, window_id):
        seen.update(channel_id=channel_id, window_id=window_id)
        return "1.1"

    monkeypatch.setattr("ccslack.handlers.toolbar.open_toolbar", _fake_open)
    await _handle_toolbar(FakeSlackClient(), "C1", "U1")
    assert seen == {"channel_id": "C1", "window_id": "@7"}


@pytest.mark.asyncio
async def test_toolbar_shortcut_rejects_unbound_channel(monkeypatch):
    called = False

    async def _fake_open(*_a, **_k):
        nonlocal called
        called = True

    monkeypatch.setattr("ccslack.handlers.toolbar.open_toolbar", _fake_open)
    client = FakeSlackClient()
    await _handle_toolbar(client, "C_UNBOUND", "U1")
    assert not called
    assert "session channel" in client.last_call("chat_postEphemeral").kwargs["text"]
