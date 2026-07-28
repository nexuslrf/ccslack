import asyncio
from unittest.mock import patch

import pytest

from ccslack.handlers import interactive
from ccslack.handlers.interactive import (
    INTERACTIVE_TOOL_NAMES,
    _hash_pane,
    _result_ts,
    enter_interactive_mode,
    exit_for_window,
    exit_interactive_mode,
    is_in_interactive_mode,
    maybe_exit_for_tool_result,
    session_for_window,
)
from ccslack.slack_client import FakeSlackClient


@pytest.fixture(autouse=True)
def _clear_interactive_state():
    interactive._active.clear()
    yield
    interactive._active.clear()


@pytest.fixture
def fake_client():
    client = FakeSlackClient()
    client.returns["chat_postMessage"] = {"ok": True, "ts": "1700000000.000100"}
    client.returns["chat_update"] = {"ok": True}
    return client


def test_interactive_tool_names_includes_codex_and_claude():
    assert "AskUserQuestion" in INTERACTIVE_TOOL_NAMES
    assert "ExitPlanMode" in INTERACTIVE_TOOL_NAMES
    assert "request_user_input" in INTERACTIVE_TOOL_NAMES


def test_hash_pane_is_deterministic():
    assert _hash_pane("hello") == _hash_pane("hello")
    assert _hash_pane("hello") != _hash_pane("world")


def test_result_ts_handles_dict_and_missing():
    assert _result_ts({"ts": "1234.5"}) == "1234.5"
    assert _result_ts({"ok": True}) == ""
    assert _result_ts(None) == ""


@pytest.mark.asyncio
async def test_enter_posts_message_and_starts_refresh(fake_client):
    async def fake_capture(_window_id):
        return "Select option:\n❯ 1. Yes\n  2. No"

    with patch.object(interactive, "_capture_pane_snippet", side_effect=fake_capture):
        await enter_interactive_mode(
            fake_client,
            channel_id="C0SESS",
            window_id="@7",
            tool_use_id="tu_123",
            tool_name="AskUserQuestion",
        )

    assert is_in_interactive_mode("C0SESS")
    session = session_for_window("@7")
    assert session is not None
    assert session.tool_use_id == "tu_123"
    assert session.message_ts == "1700000000.000100"
    assert fake_client.call_count("chat_postMessage") == 1

    # Cancel the refresh task so the test doesn't leak it.
    if session.refresh_task is not None:
        session.refresh_task.cancel()
        await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_maybe_exit_for_tool_result_closes_matching_session(fake_client):
    async def fake_capture(_window_id):
        return "pane"

    with patch.object(interactive, "_capture_pane_snippet", side_effect=fake_capture):
        await enter_interactive_mode(
            fake_client,
            channel_id="C0SESS",
            window_id="@7",
            tool_use_id="tu_match",
            tool_name="ExitPlanMode",
        )
        assert is_in_interactive_mode("C0SESS")

        # Unknown tool_use_id should NOT close.
        assert await maybe_exit_for_tool_result(fake_client, "tu_other") is False
        assert is_in_interactive_mode("C0SESS")

        # Matching one closes the session.
        assert await maybe_exit_for_tool_result(fake_client, "tu_match") is True
        assert not is_in_interactive_mode("C0SESS")
        # ccgram's pattern: delete the picker on close (no terminal-state stub).
        assert fake_client.call_count("chat_delete") >= 1


@pytest.mark.asyncio
async def test_exit_for_window_closes_by_window_id(fake_client):
    async def fake_capture(_window_id):
        return "pane"

    with patch.object(interactive, "_capture_pane_snippet", side_effect=fake_capture):
        await enter_interactive_mode(
            fake_client,
            channel_id="C0SESS",
            window_id="@9",
            tool_use_id=None,
            tool_name="permission",
        )
        assert await exit_for_window(fake_client, "@nope", reason="x") is False
        assert is_in_interactive_mode("C0SESS")

        assert await exit_for_window(fake_client, "@9", reason="agent stop") is True
        assert not is_in_interactive_mode("C0SESS")


@pytest.mark.asyncio
async def test_exit_interactive_mode_is_idempotent(fake_client):
    # No session — exit returns False but doesn't raise.
    assert await exit_interactive_mode(fake_client, "C0EMPTY") is False


# ── keep-picker-newest (bump-to-bottom) ──────────────────────────────────────


async def _enter(client, *, window="@7", tool_use_id="tu_1", tool="AskUserQuestion"):
    async def fake_capture(_window_id):
        return "pane"

    with patch.object(interactive, "_capture_pane_snippet", side_effect=fake_capture):
        await enter_interactive_mode(
            client,
            channel_id="C0SESS",
            window_id=window,
            tool_use_id=tool_use_id,
            tool_name=tool,
        )


def _stop_refresh(session):
    if session and session.refresh_task is not None:
        session.refresh_task.cancel()


def test_note_channel_post_marks_buried(fake_client):
    interactive._active["C0SESS"] = interactive.InteractiveSession(
        channel_id="C0SESS",
        window_id="@7",
        message_ts="1.1",
        tool_use_id="tu_1",
        tool_name="AskUserQuestion",
        started_at=0.0,
        last_change_at=0.0,
    )
    assert interactive._active["C0SESS"].buried is False
    interactive.note_channel_post("C0SESS")
    assert interactive._active["C0SESS"].buried is True
    # No-op for a channel without a live picker.
    interactive.note_channel_post("C0OTHER")


@pytest.mark.asyncio
async def test_new_prompt_reposts_at_bottom(fake_client):
    await _enter(fake_client, tool_use_id="tu_1")
    session = session_for_window("@7")
    assert session.message_ts == "1700000000.000100"

    fake_client.returns["chat_postMessage"] = {"ok": True, "ts": "1700000000.000200"}
    await _enter(fake_client, tool_use_id="tu_2")  # different prompt, same window

    session = session_for_window("@7")
    assert session.message_ts == "1700000000.000200"  # moved to the new message
    assert session.tool_use_id == "tu_2"
    assert fake_client.call_count("chat_postMessage") == 2
    # Old message deleted so its stale buttons can't drive the pane.
    deleted = [c.kwargs["ts"] for c in fake_client.calls if c.method == "chat_delete"]
    assert "1700000000.000100" in deleted
    _stop_refresh(session)


@pytest.mark.asyncio
async def test_buried_same_prompt_reposts_at_bottom(fake_client):
    await _enter(fake_client, tool_use_id="tu_1")
    session = session_for_window("@7")
    session.buried = True

    fake_client.returns["chat_postMessage"] = {"ok": True, "ts": "1700000000.000200"}
    await _enter(fake_client, tool_use_id="tu_1")  # same prompt, but buried

    session = session_for_window("@7")
    assert session.message_ts == "1700000000.000200"
    assert session.buried is False
    assert fake_client.call_count("chat_postMessage") == 2
    _stop_refresh(session)


@pytest.mark.asyncio
async def test_unburied_same_prompt_edits_in_place(fake_client):
    await _enter(fake_client, tool_use_id="tu_1")

    # Same prompt, not buried, but pane changed → edit in place, no new message.
    async def fake_capture(_window_id):
        return "different pane"

    with patch.object(interactive, "_capture_pane_snippet", side_effect=fake_capture):
        await enter_interactive_mode(
            fake_client,
            channel_id="C0SESS",
            window_id="@7",
            tool_use_id="tu_1",
            tool_name="AskUserQuestion",
        )

    assert fake_client.call_count("chat_postMessage") == 1  # no repost
    assert fake_client.call_count("chat_update") >= 1
    _stop_refresh(session_for_window("@7"))
