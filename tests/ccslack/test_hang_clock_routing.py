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


def test_recurring_user_prompt_collapses_after_first(monkeypatch):
    """A scheduled prompt (same first line, timestamp inside) echoes in full
    once, then collapses to a one-liner for each later firing."""
    from ccslack.handlers.messaging_pipeline import message_routing as mr

    mr._SEEN_USER_PROMPTS.clear()

    prompt_a = "Recurring R2-B progress report. Standby:\n" + "detail " * 60
    prompt_a2 = "Recurring R2-B progress report. Standby:\n" + "detail " * 61

    first = mr._collapse_recurring_user_prompt("s1", prompt_a)
    assert first is None  # first occurrence echoes in full

    second = mr._collapse_recurring_user_prompt("s1", prompt_a2)
    assert second is not None and "occurrence #2" in second

    third = mr._collapse_recurring_user_prompt("s1", prompt_a)
    assert third is not None and "occurrence #3" in third


def test_short_repeats_never_collapsed():
    from ccslack.handlers.messaging_pipeline import message_routing as mr

    mr._SEEN_USER_PROMPTS.clear()
    for _ in range(5):
        assert mr._collapse_recurring_user_prompt("s2", "y") is None
        assert mr._collapse_recurring_user_prompt("s2", "continue") is None


def test_distinct_long_prompts_echo_in_full():
    from ccslack.handlers.messaging_pipeline import message_routing as mr

    mr._SEEN_USER_PROMPTS.clear()
    long_a = "Analyze the failing test suite and:\n" + "steps " * 80
    long_b = "Draft the Q3 report covering:\n" + "content " * 80
    assert mr._collapse_recurring_user_prompt("s3", long_a) is None
    assert mr._collapse_recurring_user_prompt("s3", long_b) is None


@pytest.mark.asyncio
async def test_echo_off_skips_user_echo_post(monkeypatch):
    """/ccslack echo off — user prompts reach the agent but no echo posts."""
    from ccslack.handlers.messaging_pipeline import message_routing as mr
    from ccslack.session_monitor import NewMessage
    from ccslack.slack_client import FakeSlackClient
    from ccslack.window_state_store import window_store

    monkeypatch.setattr(
        "ccslack.session_query.find_channels_for_session",
        lambda sid: [("C1", "@1")],
    )

    posted = []

    class _Client(FakeSlackClient):
        async def chat_postMessage(self, **kw):  # noqa: N802
            posted.append(kw)
            return {"ts": "1"}

    # echo OFF for the window.
    state = window_store.get_window_state("@1")
    state.echo_user_input = False

    msg = NewMessage(
        session_id="s1",
        text="my prompt",
        is_complete=True,
        content_type="text",
        role="user",
    )
    await mr.handle_new_message(msg, _Client())
    assert posted == []  # no echo

    # echo back ON → the echo posts (exactly one — the visible echo).
    state.echo_user_input = True
    await mr.handle_new_message(msg, _Client())
    echo_posts = [p for p in posted if "my prompt" in (p.get("text") or "")]
    assert len(echo_posts) == 1


def test_echo_state_persists_roundtrip():
    from ccslack.window_state_store import WindowState

    ws = WindowState()
    ws.echo_user_input = False
    d = ws.to_dict()
    assert d["echo_user_input"] is False
    restored = WindowState.from_dict(d)
    assert restored.echo_user_input is False
    # Default roundtrip: absent field → True.
    assert WindowState.from_dict({}).echo_user_input is True
