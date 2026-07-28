from pathlib import Path

import pytest

from ccslack.handlers import file_refs
from ccslack.handlers.file_refs import (
    _candidates,
    find_file_refs,
    maybe_offer_file_refs,
)
from ccslack.slack_client import FakeSlackClient


@pytest.fixture
def tree(tmp_path: Path) -> Path:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "app.py").write_text("print(1)")
    (tmp_path / "report.md").write_text("# hi")
    (tmp_path / ".env").write_text("SECRET=1")
    return tmp_path


def _action_ids(blocks: list[dict]) -> list[str]:
    ids: list[str] = []
    for b in blocks:
        for el in b.get("elements", []):
            ids.append(el.get("action_id", ""))
    return ids


# ── detection ────────────────────────────────────────────────────────────────


def test_candidates_from_backticks_and_bare():
    cands = _candidates("Edited `src/app.py` and also report.md now.")
    assert "src/app.py" in cands
    assert "report.md" in cands


def test_candidates_skip_urls_and_plain_words():
    cands = _candidates("See https://example.com/x and the word hello there")
    assert not any("://" in c for c in cands)
    assert "hello" not in cands


def test_candidates_slack_link_markup_takes_target():
    cands = _candidates("open </a/b/prepare.py:722|prepare.py> now")
    assert "/a/b/prepare.py" in cands  # full target, not just the label


def test_candidates_bare_absolute_path():
    assert "/abs/path/foo.py" in _candidates("wrote /abs/path/foo.py done")


def test_candidates_strip_line_and_column_suffix():
    cands = _candidates("edit src/app.py:722 and lib/x.py:12:5")
    assert "src/app.py" in cands
    assert "lib/x.py" in cands


def test_finds_referenced_files(tree: Path):
    text = "I created `src/app.py` and updated report.md."
    found = {p.name for p in find_file_refs(text, tree)}
    assert found == {"app.py", "report.md"}


def test_relative_and_dotslash_paths(tree: Path):
    found = {str(p.resolve()) for p in find_file_refs("see ./src/app.py", tree)}
    assert str((tree / "src" / "app.py").resolve()) in found


def test_slack_link_with_line_suffix_under_cwd(tree: Path):
    abs_path = (tree / "src" / "app.py").resolve()
    text = f"see <{abs_path}:722|app.py> for the change"
    found = {p.name for p in find_file_refs(text, tree)}
    assert "app.py" in found


def test_nonexistent_paths_dropped(tree: Path):
    assert find_file_refs("look at `src/missing.py` and nope.txt", tree) == []


def test_hidden_and_secret_files_excluded(tree: Path):
    # .env exists but is hidden + matches a secret pattern → never surfaced.
    assert find_file_refs("check `.env` for the key", tree) == []


def test_directory_reference_not_a_file(tree: Path):
    assert find_file_refs("the `src/` directory", tree) == []


def test_dedupes_by_absolute_path(tree: Path):
    text = "`src/app.py` then again src/app.py and ./src/app.py"
    assert len(find_file_refs(text, tree)) == 1


def test_outside_cwd_rejected(tree: Path, tmp_path: Path):
    outside = tmp_path.parent / "outside.txt"
    outside.write_text("x")
    try:
        assert find_file_refs(f"grab `{outside}`", tree) == []
    finally:
        outside.unlink()


# ── offer ────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_offer_posts_show_files_button(tree: Path, monkeypatch):
    monkeypatch.setattr("ccslack.handlers.send._resolve_cwd", lambda _c: tree)
    client = FakeSlackClient()
    await maybe_offer_file_refs(client, "C1", "made `src/app.py`")
    call = client.last_call("chat_postMessage")
    assert "ccslack_show_files" in _action_ids(call.kwargs["blocks"])


@pytest.mark.asyncio
async def test_offer_silent_when_no_files(tree: Path, monkeypatch):
    monkeypatch.setattr("ccslack.handlers.send._resolve_cwd", lambda _c: tree)
    client = FakeSlackClient()
    await maybe_offer_file_refs(client, "C1", "no files here, just prose")
    assert client.call_count("chat_postMessage") == 0


@pytest.mark.asyncio
async def test_offer_respects_disable_flag(tree: Path, monkeypatch):
    monkeypatch.setattr(file_refs.config, "file_refs_offer", False)
    monkeypatch.setattr("ccslack.handlers.send._resolve_cwd", lambda _c: tree)
    client = FakeSlackClient()
    await maybe_offer_file_refs(client, "C1", "made `src/app.py`")
    assert client.call_count("chat_postMessage") == 0
