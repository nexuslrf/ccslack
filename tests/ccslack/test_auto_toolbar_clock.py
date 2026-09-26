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


@pytest.mark.asyncio
async def test_slash_command_does_not_seed_hang_clock(monkeypatch):
    """Agent-local commands (/status, /usage, /resume, …) must NOT start the
    hang clock — they're instant TUI actions, and no transcript output follows,
    so seeding the clock false-alarms the toolbar 2 min later."""
    from ccslack.handlers import agent_input
    from ccslack.window_state_store import window_store

    coord._auto_toolbar.clear()

    async def _noop_send(*a, **kw):
        return None

    monkeypatch.setattr(agent_input.tmux_manager, "send_keys", _noop_send)
    monkeypatch.setattr(
        agent_input.shell_capture,
        "is_shell_window",
        staticmethod(lambda _: False),
    )

    async def _fake_open_toolbar(_client, _channel, _wid):
        pass

    monkeypatch.setattr("ccslack.handlers.toolbar.open_toolbar", _fake_open_toolbar)

    class _FakeClient:
        async def chat_postEphemeral(self, **kw):  # noqa: N802
            return {"ok": True}

    window_store.get_window_state("@20")
    for cmd in ("/status", "/usage", "/resume", "/model", "/changelog"):
        await agent_input.deliver_to_agent(_FakeClient(), "C9", "@20", cmd)
    assert "@20" not in coord._auto_toolbar  # no clock seeded


@pytest.mark.asyncio
async def test_regular_prompt_still_seeds_hang_clock(monkeypatch):
    from ccslack.handlers import agent_input
    from ccslack.window_state_store import window_store

    coord._auto_toolbar.clear()

    async def _noop_send(*a, **kw):
        return None

    monkeypatch.setattr(agent_input.tmux_manager, "send_keys", _noop_send)
    monkeypatch.setattr(
        agent_input.shell_capture,
        "is_shell_window",
        staticmethod(lambda _: False),
    )

    class _FakeClient:
        async def chat_postEphemeral(self, **kw):  # noqa: N802
            return {"ok": True}

    window_store.get_window_state("@21")
    await agent_input.deliver_to_agent(_FakeClient(), "C9", "@21", "run the sweep")
    assert "@21" in coord._auto_toolbar  # real prompt → clock seeded


@pytest.mark.asyncio
async def test_picker_command_pops_toolbar(monkeypatch):
    """TUI-picker commands (/model for pi) open the toolbar automatically."""
    from ccslack.handlers import agent_input
    from ccslack.window_state_store import window_store

    opened = []

    async def _fake_open_toolbar(_client, _channel, wid):
        opened.append(wid)

    async def _noop_send(*a, **kw):
        return None

    monkeypatch.setattr("ccslack.handlers.toolbar.open_toolbar", _fake_open_toolbar)
    monkeypatch.setattr(agent_input.tmux_manager, "send_keys", _noop_send)
    monkeypatch.setattr(
        agent_input.shell_capture, "is_shell_window", staticmethod(lambda _: False)
    )

    class _FakeClient:
        async def chat_postEphemeral(self, **kw):  # noqa: N802
            return {"ok": True}

    # Window configured as pi → /model is a picker command for pi.
    window_store.get_window_state("@30").provider_name = "pi"
    await agent_input.deliver_to_agent(_FakeClient(), "C9", "@30", "/model")
    assert opened == ["@30"]


@pytest.mark.asyncio
async def test_non_picker_command_no_toolbar(monkeypatch):
    from ccslack.handlers import agent_input
    from ccslack.window_state_store import window_store

    opened = []

    async def _fake_open_toolbar(_client, _channel, wid):
        opened.append(wid)

    monkeypatch.setattr("ccslack.handlers.toolbar.open_toolbar", _fake_open_toolbar)

    async def _noop_send(*a, **kw):
        return None

    monkeypatch.setattr(agent_input.tmux_manager, "send_keys", _noop_send)
    monkeypatch.setattr(
        agent_input.shell_capture, "is_shell_window", staticmethod(lambda _: False)
    )

    class _FakeClient:
        async def chat_postEphemeral(self, **kw):  # noqa: N802
            return {"ok": True}

    window_store.get_window_state("@31").provider_name = "pi"
    # /changelog just prints text — not a picker.
    await agent_input.deliver_to_agent(_FakeClient(), "C9", "@31", "/changelog")
    # A plain prompt is not a command at all.
    await agent_input.deliver_to_agent(_FakeClient(), "C9", "@31", "run the sweep")
    assert opened == []


@pytest.mark.asyncio
async def test_picker_command_provider_scoped(monkeypatch):
    """A command that's a picker for pi but NOT for another provider doesn't
    pop the toolbar on that other provider's window."""
    from ccslack.handlers import agent_input
    from ccslack.window_state_store import window_store

    opened = []

    async def _fake_open_toolbar(_client, _channel, wid):
        opened.append(wid)

    monkeypatch.setattr("ccslack.handlers.toolbar.open_toolbar", _fake_open_toolbar)

    async def _noop_send(*a, **kw):
        return None

    monkeypatch.setattr(agent_input.tmux_manager, "send_keys", _noop_send)
    monkeypatch.setattr(
        agent_input.shell_capture, "is_shell_window", staticmethod(lambda _: False)
    )

    class _FakeClient:
        async def chat_postEphemeral(self, **kw):  # noqa: N802
            return {"ok": True}

    # "personality" is a codex picker command, not a pi one.
    window_store.get_window_state("@32").provider_name = "pi"
    await agent_input.deliver_to_agent(_FakeClient(), "C9", "@32", "/personality")
    assert opened == []
    window_store.get_window_state("@33").provider_name = "codex"
    await agent_input.deliver_to_agent(_FakeClient(), "C9", "@33", "/personality")
    assert opened == ["@33"]
