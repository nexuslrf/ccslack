"""Tests for the auto-toolbar hang-clock seeding semantics."""

import pytest

from ccslack.handlers.polling import coordinator as coord


@pytest.fixture(autouse=True)
def _clean():
    coord._auto_toolbar.clear()
    coord._last_activity.clear()


def test_mark_active_creates_clock():
    # Agent produced output with NO prior Slack prompt (e.g. terminal-driven):
    # mark_active must CREATE the hang clock, not just reset an existing one.
    coord.mark_active("@1")
    assert "@1" in coord._auto_toolbar
    clock, opened, notified = coord._auto_toolbar["@1"]
    assert clock > 0
    assert opened is False
    assert notified is False


def test_mark_active_resets_clock_keeps_opened():
    coord.mark_active("@1")
    # Simulate a hang that opened the toolbar.
    c, _o, _n = coord._auto_toolbar["@1"]
    coord._auto_toolbar["@1"] = (c, True, True)
    # New activity: clock resets, opened stays (user may still drive it),
    # notified resets so a LATER hang can @channel again.
    coord.mark_active("@1")
    clock, opened, notified = coord._auto_toolbar["@1"]
    assert clock >= c
    assert opened is True
    assert notified is False


def test_mark_agent_stuck_creates_clock():
    # Notification hook: agent explicitly waiting for input.
    coord.mark_agent_stuck("@2")
    assert "@2" in coord._auto_toolbar
    clock, opened, _ = coord._auto_toolbar["@2"]
    assert clock > 0
    assert opened is False


def test_mark_agent_stuck_keeps_open_toolbar():
    coord.mark_agent_stuck("@2")
    c, _o, _n = coord._auto_toolbar["@2"]
    coord._auto_toolbar["@2"] = (c, True, False)
    coord.mark_agent_stuck("@2")
    _, opened, _ = coord._auto_toolbar["@2"]
    assert opened is True


def test_stuck_pane_regex_matches_approval_chrome():
    assert coord._STUCK_PANE_RE.search("Do you want to allow Claude to fetch?")
    assert coord._STUCK_PANE_RE.search("Permission rule WebFetch requires confirmation")
    assert coord._STUCK_PANE_RE.search("Do you want to make this edit to foo.py?")


def test_stuck_pane_regex_ignores_normal_output():
    assert not coord._STUCK_PANE_RE.search("Ran 42 tests, all passed in 3.2s")
    assert not coord._STUCK_PANE_RE.search("Here is the summary of the changes")


def test_notification_filter_blocks_idle_reminder():
    """Claude's idle reminder must NOT re-seed the hang clock after end_turn."""
    import asyncio

    from ccslack.handlers.hook_events import dispatch_hook_event

    class _Evt:
        event_type = "Notification"
        window_key = "ccslack:@3"
        session_id = "s"
        data = {"message": "Claude is waiting for your input"}
        timestamp = 0.0

    class _Client:
        async def chat_postMessage(self, **kw):  # noqa: N802
            return {"ts": "1"}

    import ccslack.thread_router as tr
    tr.thread_router.bind_channel("C3", "@3", window_name="t")
    coord._auto_toolbar.pop("@3", None)

    asyncio.run(dispatch_hook_event(_Evt(), _Client()))
    assert "@3" not in coord._auto_toolbar  # idle reminder → no seed


def test_notification_filter_seeds_permission():
    """A permission notification seeds the hang clock."""
    import asyncio

    from ccslack.handlers.hook_events import dispatch_hook_event

    class _Evt:
        event_type = "Notification"
        window_key = "ccslack:@4"
        session_id = "s"
        data = {"message": "Claude needs your permission to use WebFetch"}
        timestamp = 0.0

    class _Client:
        async def chat_postMessage(self, **kw):  # noqa: N802
            return {"ts": "1"}

    import ccslack.thread_router as tr
    tr.thread_router.bind_channel("C4", "@4", window_name="t")
    coord._auto_toolbar.pop("@4", None)

    asyncio.run(dispatch_hook_event(_Evt(), _Client()))
    assert "@4" in coord._auto_toolbar  # permission → seeded
