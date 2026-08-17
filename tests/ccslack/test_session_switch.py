"""Tests for session-switch detection on /resume from Slack."""

import pytest

from ccslack.handlers.agent_input import _is_session_switch_command


def test_resume_detected():
    assert _is_session_switch_command("/resume") is True


def test_fork_detected():
    assert _is_session_switch_command("/fork") is True


def test_clone_detected():
    assert _is_session_switch_command("/clone") is True


def test_import_detected():
    assert _is_session_switch_command("/import") is True


def test_case_insensitive():
    assert _is_session_switch_command("/RESUME") is True
    assert _is_session_switch_command("/Resume") is True


def test_resume_with_args_detected():
    assert _is_session_switch_command("/resume --last") is True


def test_normal_text_not_detected():
    assert _is_session_switch_command("hello world") is False
    assert _is_session_switch_command("fix the bug") is False


def test_ccslack_commands_not_detected():
    assert _is_session_switch_command("/ccslack resume") is False
    assert _is_session_switch_command("/ccslack kill") is False


def test_new_not_detected():
    # /new collides with bot-native /ccslack new — must not trigger switch.
    assert _is_session_switch_command("/new") is False


def test_empty_text_not_detected():
    assert _is_session_switch_command("") is False
    assert _is_session_switch_command("   ") is False


@pytest.mark.asyncio
async def test_deliver_sets_switch_pending(monkeypatch):
    """deliver_to_agent flags the window when /resume is forwarded."""
    from ccslack.handlers import agent_input
    from ccslack.window_state_store import window_store

    # Stub tmux send_keys so no real tmux is needed.
    async def _noop_send(*a, **kw):
        return None

    monkeypatch.setattr(agent_input.tmux_manager, "send_keys", _noop_send)
    monkeypatch.setattr(agent_input, "shell_capture", type("S", (), {"is_shell_window": staticmethod(lambda _: False)})())
    monkeypatch.setattr(agent_input, "shell_marker", type("M", (), {"has_marker": staticmethod(lambda _: False)})())

    # Ensure the window exists in the store.
    store = window_store
    store.get_window_state("@9")  # creates if absent
    store.window_states["@9"].session_id = "session-A"

    class _FakeClient:
        async def chat_postEphemeral(self, **kw):  # noqa: N802
            return {"ok": True}

    await agent_input.deliver_to_agent(_FakeClient(), "C1", "@9", "/resume")

    state = store.window_states.get("@9")
    assert state is not None
    assert state.session_switch_from != ""
    assert state.session_switch_at > 0


@pytest.mark.asyncio
async def test_deliver_does_not_flag_normal_text(monkeypatch):
    from ccslack.handlers import agent_input
    from ccslack.window_state_store import window_store

    async def _noop_send(*a, **kw):
        return None

    monkeypatch.setattr(agent_input.tmux_manager, "send_keys", _noop_send)
    monkeypatch.setattr(agent_input, "shell_capture", type("S", (), {"is_shell_window": staticmethod(lambda _: False)})())
    monkeypatch.setattr(agent_input, "shell_marker", type("M", (), {"has_marker": staticmethod(lambda _: False)})())

    store = window_store
    store.get_window_state("@10")

    class _FakeClient:
        async def chat_postEphemeral(self, **kw):  # noqa: N802
            return {"ok": True}

    await agent_input.deliver_to_agent(_FakeClient(), "C1", "@10", "hello")

    state = store.window_states.get("@10")
    assert state is not None
    assert state.session_switch_from == ""
    assert state.session_switch_at == 0.0


def test_set_and_clear_switch_pending():
    from ccslack.window_state_store import window_store

    store = window_store
    state = store.get_window_state("@11")
    original_sid = state.session_id

    store.set_session_switch_pending("@11")
    assert state.session_switch_from == original_sid
    assert state.session_switch_at > 0

    store.clear_session_switch_pending("@11")
    assert state.session_switch_from == ""
    assert state.session_switch_at == 0.0
