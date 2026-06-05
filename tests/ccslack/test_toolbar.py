import asyncio

import pytest

from ccslack.handlers import toolbar
from ccslack.session import session_manager
from ccslack.slack_client import FakeSlackClient


def _has_codeblock(blocks: list[dict], needle: str) -> bool:
    for block in blocks:
        if block.get("type") != "section":
            continue
        text = block.get("text", {}).get("text", "")
        if text.startswith("```") and needle in text:
            return True
    return False


def test_build_toolbar_blocks_includes_pane_codeblock():
    session_manager.set_window_provider("@30", "claude", cwd="/tmp")
    blocks, _ = toolbar.build_toolbar_blocks("@30", "hello from the pane")
    assert _has_codeblock(blocks, "hello from the pane")


def test_build_toolbar_blocks_without_pane_omits_codeblock():
    session_manager.set_window_provider("@31", "claude", cwd="/tmp")
    blocks, _ = toolbar.build_toolbar_blocks("@31")
    assert not any(
        b.get("type") == "section"
        and b.get("text", {}).get("text", "").startswith("```")
        for b in blocks
    )


@pytest.mark.asyncio
async def test_open_toolbar_posts_live_text_and_starts_refresh(monkeypatch):
    session_manager.set_window_provider("@32", "claude", cwd="/tmp")

    panes = iter(["first pane", "first pane", "second pane"])

    async def fake_snippet(window_id):  # noqa: ARG001
        return next(panes, "second pane")

    async def fake_find(window_id):  # noqa: ARG001
        return object()

    monkeypatch.setattr(toolbar, "_capture_pane_snippet", fake_snippet)
    monkeypatch.setattr(toolbar.tmux_manager, "find_window_by_id", fake_find)
    monkeypatch.setattr(toolbar, "REFRESH_INTERVAL", 0.01)

    client = FakeSlackClient()
    client.returns["chat_postMessage"] = {"ok": True, "ts": "111.222"}

    ts = await toolbar.open_toolbar(client, "C0LIVE", "@32")
    assert ts == "111.222"

    post = client.last_call("chat_postMessage")
    assert _has_codeblock(post.kwargs["blocks"], "first pane")

    # Refresh loop should chat.update once the pane changes.
    for _ in range(20):
        await asyncio.sleep(0.01)
        if client.call_count("chat_update"):
            break
    assert client.call_count("chat_update") >= 1
    update = client.last_call("chat_update")
    assert _has_codeblock(update.kwargs["blocks"], "second pane")

    toolbar._stop_refresh(ts)
    assert ts not in toolbar._active_toolbars


@pytest.mark.asyncio
async def test_refresh_loop_stops_when_window_dies(monkeypatch):
    session_manager.set_window_provider("@33", "claude", cwd="/tmp")

    async def fake_snippet(window_id):  # noqa: ARG001
        return "pane"

    async def dead_window(window_id):  # noqa: ARG001
        return None

    monkeypatch.setattr(toolbar, "_capture_pane_snippet", fake_snippet)
    monkeypatch.setattr(toolbar.tmux_manager, "find_window_by_id", dead_window)
    monkeypatch.setattr(toolbar, "REFRESH_INTERVAL", 0.01)

    client = FakeSlackClient()
    client.returns["chat_postMessage"] = {"ok": True, "ts": "333.444"}

    ts = await toolbar.open_toolbar(client, "C0DEAD", "@33")
    for _ in range(20):
        await asyncio.sleep(0.01)
        if ts not in toolbar._active_toolbars:
            break
    assert ts not in toolbar._active_toolbars
