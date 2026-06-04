import pytest

from ccslack import window_query
from ccslack.config import config
from ccslack.session import session_manager
from ccslack.window_state_store import window_store


@pytest.fixture
def seeded():
    window_store.window_states.clear()
    session_manager.set_window_provider("@1", "claude", cwd="/tmp/x")
    yield
    window_store.window_states.clear()


def test_default_follows_global_true(seeded, monkeypatch):
    monkeypatch.setattr(config, "thread_tool_calls", True)
    assert window_query.get_thread_tool_calls("@1") == "default"
    assert window_query.is_tool_threading_enabled("@1") is True


def test_default_follows_global_false(seeded, monkeypatch):
    monkeypatch.setattr(config, "thread_tool_calls", False)
    assert window_query.is_tool_threading_enabled("@1") is False


def test_on_overrides_global_false(seeded, monkeypatch):
    monkeypatch.setattr(config, "thread_tool_calls", False)
    session_manager.set_thread_tool_calls("@1", "on")
    assert window_query.is_tool_threading_enabled("@1") is True


def test_off_overrides_global_true(seeded, monkeypatch):
    monkeypatch.setattr(config, "thread_tool_calls", True)
    session_manager.set_thread_tool_calls("@1", "off")
    assert window_query.is_tool_threading_enabled("@1") is False


def test_cycle_order(seeded):
    assert session_manager.cycle_thread_tool_calls("@1") == "on"
    assert session_manager.cycle_thread_tool_calls("@1") == "off"
    assert session_manager.cycle_thread_tool_calls("@1") == "default"


def test_invalid_mode_rejected(seeded):
    with pytest.raises(ValueError, match="Invalid thread_tool_calls"):
        session_manager.set_thread_tool_calls("@1", "bogus")


def test_unknown_window_uses_global(monkeypatch):
    window_store.window_states.clear()
    monkeypatch.setattr(config, "thread_tool_calls", True)
    assert window_query.is_tool_threading_enabled("@999") is True


def test_thread_mode_survives_serialization(seeded):
    session_manager.set_thread_tool_calls("@1", "off")
    d = window_store.window_states["@1"].to_dict()
    assert d["thread_tool_calls"] == "off"
    from ccslack.window_state_store import WindowState

    restored = WindowState.from_dict(d)
    assert restored.thread_tool_calls == "off"
