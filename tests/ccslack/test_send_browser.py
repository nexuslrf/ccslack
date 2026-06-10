from pathlib import Path

import pytest

from ccslack.handlers.send import (
    _build_browser_view,
    _dispatch_browse,
    _list_dir,
    _within,
    handle_send,
)
from ccslack.session import session_manager
from ccslack.slack_client import FakeSlackClient
from ccslack.thread_router import thread_router
from ccslack.window_state_store import window_store


@pytest.fixture
def tree(tmp_path: Path) -> Path:
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "arch.png").write_bytes(b"i")
    (tmp_path / "src").mkdir()
    (tmp_path / "readme.md").write_text("hi")
    (tmp_path / ".secret").write_text("x")  # hidden — dropped from the listing
    return tmp_path


@pytest.fixture(autouse=True)
def _clean():
    window_store.window_states.clear()
    thread_router.reset()
    yield
    window_store.window_states.clear()
    thread_router.reset()


def _action_ids(blocks: list[dict]) -> list[str]:
    ids: list[str] = []
    for block in blocks:
        if block.get("type") == "actions":
            ids.extend(e["action_id"] for e in block["elements"])
    return ids


def _elements(blocks: list[dict]) -> list[dict]:
    out: list[dict] = []
    for block in blocks:
        if block.get("type") == "actions":
            out.extend(block["elements"])
    return out


def test_within():
    root = Path("/a/b")
    assert _within(Path("/a/b"), root)
    assert _within(Path("/a/b/c"), root)
    assert not _within(Path("/a"), root)
    assert not _within(Path("/x"), root)


def test_list_dir_drops_hidden_and_orders_dirs_first(tree: Path):
    dirs, files = _list_dir(tree)
    assert sorted(d.name for d in dirs) == ["docs", "src"]
    assert [f.name for f in files] == ["readme.md"]


def test_browser_lists_folders_and_files(tree: Path):
    blocks, _ = _build_browser_view(tree, tree)
    ids = _action_ids(blocks)
    assert any(i.startswith("ccslack_send_browse:") for i in ids)
    assert any(i.startswith("ccslack_send_pick:") for i in ids)
    assert "ccslack_send_browse:up" not in ids  # no up button at root
    assert "2 folder(s), 1 file(s)" in blocks[0]["text"]["text"]


def test_browser_button_values_are_abs_paths(tree: Path):
    blocks, _ = _build_browser_view(tree, tree)
    values = [e["value"] for e in _elements(blocks)]
    assert str((tree / "docs").resolve()) in values
    assert str((tree / "readme.md").resolve()) in values


def test_browser_subdir_has_up_button_to_parent(tree: Path):
    blocks, _ = _build_browser_view(tree / "docs", tree)
    up = [e for e in _elements(blocks) if e["action_id"] == "ccslack_send_browse:up"]
    assert len(up) == 1
    assert up[0]["value"] == str(tree.resolve())


def test_browser_contains_navigation_to_cwd(tree: Path):
    # A target above the cwd resets to the cwd root (no up button there).
    blocks, _ = _build_browser_view(tree.parent, tree)
    assert "ccslack_send_browse:up" not in _action_ids(blocks)


def test_empty_folder_renders_without_buttons(tree: Path):
    blocks, _ = _build_browser_view(tree / "src", tree)
    assert _action_ids(blocks) == ["ccslack_send_browse:up"]
    assert any("empty folder" in str(b) for b in blocks)


@pytest.mark.asyncio
async def test_send_no_arg_opens_browser(tree: Path):
    session_manager.set_window_provider("@1", "claude", cwd=str(tree))
    thread_router.bind_channel("C1", "@1", window_name="proj")
    client = FakeSlackClient()

    await handle_send(client, "C1", "U1", "")

    eph = client.last_call("chat_postEphemeral")
    assert eph is not None
    ids = _action_ids(eph.kwargs["blocks"])
    assert any(i.startswith("ccslack_send_browse:") for i in ids)


@pytest.mark.asyncio
async def test_dispatch_browse_replaces_original(tree: Path):
    session_manager.set_window_provider("@2", "claude", cwd=str(tree))
    thread_router.bind_channel("C2", "@2", window_name="proj")

    captured: dict = {}

    async def _respond(**kwargs):
        captured.update(kwargs)

    body = {
        "user": {"id": "U1"},
        "channel": {"id": "C2"},
        "actions": [
            {
                "action_id": "ccslack_send_browse:0",
                "value": str((tree / "docs").resolve()),
            }
        ],
    }

    await _dispatch_browse(body, _respond)

    assert captured.get("replace_original") is True
    ids = _action_ids(captured["blocks"])
    assert "ccslack_send_browse:up" in ids  # we navigated into docs/
    # docs/ contains arch.png as a sendable file button
    values = [e["value"] for e in _elements(captured["blocks"])]
    assert str((tree / "docs" / "arch.png").resolve()) in values
