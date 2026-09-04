"""Tests for hang-clock reset semantics and defensive offers in routing."""

import pytest

from ccslack.handlers.messaging_pipeline import message_routing as mr


def test_safe_offer_swallows_exceptions(caplog):
    """A crashing cosmetic offer must not kill routing (it used to leave a
    stale hang clock → false-alarm toolbar + @channel)."""

    async def _bad(_client, _channel, _text):
        raise RuntimeError("boom")

    async def _ok(_client, _channel, _text):
        return None

    class _C:
        pass

    import asyncio

    # Raises inside → swallowed with a warning.
    asyncio.run(mr._safe_offer(_bad, "test", _C(), "C1", "x"))
    # Clean run → fine.
    asyncio.run(mr._safe_offer(_ok, "test", _C(), "C1", "x"))


def test_file_refs_resolve_none_on_expanduser_crash(monkeypatch, tmp_path):
    """A `~`-token whose expanduser raises must be skipped, not crash."""
    from ccslack.handlers import file_refs

    def _raise(self):
        raise RuntimeError("Could not determine home directory.")

    monkeypatch.setattr("pathlib.Path.expanduser", _raise)
    result = file_refs.find_file_refs("see ~/notes.md and readme.md", tmp_path)
    assert isinstance(result, list)  # no crash


@pytest.mark.asyncio
async def test_tool_result_resets_hang_clock(monkeypatch):
    """Tool events mark the agent as working — long tool chains must not
    false-alarm the auto-toolbar."""
    from ccslack.handlers.polling import coordinator as coord
    from ccslack.session_monitor import NewMessage
    from ccslack.slack_client import FakeSlackClient

    coord._auto_toolbar.clear()
    coord._last_activity.clear()
    calls = []

    def _fake_mark_active(wid):
        calls.append(wid)

    monkeypatch.setattr(
        "ccslack.handlers.polling.coordinator.mark_active", _fake_mark_active
    )
    monkeypatch.setattr(
        "ccslack.session_query.find_channels_for_session",
        lambda sid: [("C1", "@1")],
    )
    async def _noop_status(*_a, **_k):
        return None

    monkeypatch.setattr("ccslack.handlers.status.update_status", _noop_status)

    msg = NewMessage(
        session_id="s1",
        text="done",
        is_complete=True,
        content_type="tool_result",
        role="assistant",
        tool_use_id="t1",
        tool_name="Bash",
    )
    await mr.handle_new_message(msg, FakeSlackClient())
    assert "@1" in calls  # tool_result reset the clock


@pytest.mark.asyncio
async def test_short_thinking_still_routed_to_clock(monkeypatch):
    """Even short thinking (dropped from posting) must reset the clock before
    the early return in handle_new_message."""
    from ccslack.session_monitor import NewMessage
    from ccslack.slack_client import FakeSlackClient

    calls = []

    def _fake_mark_active(wid):
        calls.append(wid)

    monkeypatch.setattr(
        "ccslack.handlers.polling.coordinator.mark_active", _fake_mark_active
    )
    monkeypatch.setattr(
        "ccslack.session_query.find_channels_for_session",
        lambda sid: [("C1", "@1")],
    )

    msg = NewMessage(
        session_id="s1",
        text="hmm",  # < 20 chars — dropped from posting by the thinking filter
        is_complete=True,
        content_type="thinking",
        role="assistant",
    )
    await mr.handle_new_message(msg, FakeSlackClient())
    assert "@1" in calls  # clock reset even though the snippet was not posted
