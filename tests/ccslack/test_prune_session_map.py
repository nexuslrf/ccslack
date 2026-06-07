import json
import pytest

from ccslack.session import session_manager
from ccslack.session_map import session_map_sync
from ccslack.thread_router import thread_router
from ccslack.window_state_store import window_store


def _bind(channel_id: str, window_id: str, cwd: str = "/proj") -> None:
    session_manager.set_window_provider(window_id, "claude", cwd=cwd)
    thread_router.bind_channel(channel_id, window_id, window_name="proj")


@pytest.fixture(autouse=True)
def _clean(tmp_path, monkeypatch):
    from ccslack import config as cfg_mod

    monkeypatch.setattr(
        cfg_mod.config,
        "session_map_file",
        tmp_path / "session_map.json",
    )
    window_store.window_states.clear()
    thread_router.reset()
    yield
    window_store.window_states.clear()
    thread_router.reset()


def _write_session_map(path, entries: dict) -> None:
    path.write_text(json.dumps(entries))


def test_prune_removes_state_for_unbound_dead_window(tmp_path, monkeypatch):
    from ccslack import config as cfg_mod

    monkeypatch.setattr(cfg_mod.config, "tmux_session_name", "ccslack")
    session_map_path = cfg_mod.config.session_map_file
    session_manager.set_window_provider("@99", "claude", cwd="/unbound")
    _write_session_map(
        session_map_path, {"ccslack:@99": {"session_id": "s1", "cwd": "/unbound"}}
    )

    session_map_sync.prune_session_map(live_window_ids=set())

    assert not window_store.has_window("@99")


def test_prune_keeps_state_for_bound_dead_window(tmp_path, monkeypatch):
    from ccslack import config as cfg_mod

    monkeypatch.setattr(cfg_mod.config, "tmux_session_name", "ccslack")
    session_map_path = cfg_mod.config.session_map_file
    _bind("C0BOUND", "@100", cwd="/myproject")
    _write_session_map(
        session_map_path,
        {"ccslack:@100": {"session_id": "s2", "cwd": "/myproject"}},
    )

    session_map_sync.prune_session_map(live_window_ids=set())

    assert window_store.has_window("@100")
    state = window_store.window_states["@100"]
    assert state.cwd == "/myproject"


def test_prune_removes_json_entry_even_for_bound_window(tmp_path, monkeypatch):
    from ccslack import config as cfg_mod

    monkeypatch.setattr(cfg_mod.config, "tmux_session_name", "ccslack")
    session_map_path = cfg_mod.config.session_map_file
    _bind("C0BOUND2", "@101", cwd="/proj2")
    _write_session_map(
        session_map_path,
        {"ccslack:@101": {"session_id": "s3", "cwd": "/proj2"}},
    )

    session_map_sync.prune_session_map(live_window_ids=set())

    remaining = json.loads(session_map_path.read_text())
    assert "ccslack:@101" not in remaining


def test_prune_skips_live_windows(tmp_path, monkeypatch):
    from ccslack import config as cfg_mod

    monkeypatch.setattr(cfg_mod.config, "tmux_session_name", "ccslack")
    session_map_path = cfg_mod.config.session_map_file
    session_manager.set_window_provider("@200", "codex", cwd="/live")
    _write_session_map(
        session_map_path, {"ccslack:@200": {"session_id": "s4", "cwd": "/live"}}
    )

    session_map_sync.prune_session_map(live_window_ids={"@200"})

    assert window_store.has_window("@200")
    remaining = json.loads(session_map_path.read_text())
    assert "ccslack:@200" in remaining
