from pathlib import Path

import pytest

from ccslack.handlers.send import (
    _find_files,
    _is_image,
    _safe_relative,
    _walk_filtered,
)


@pytest.fixture
def tree(tmp_path: Path) -> Path:
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "arch.png").write_bytes(b"img")
    (tmp_path / "docs" / "readme.md").write_text("hi")
    (tmp_path / "diagram.png").write_bytes(b"img2")
    (tmp_path / "notes.txt").write_text("x")
    # excluded dir should be pruned
    (tmp_path / "node_modules").mkdir()
    (tmp_path / "node_modules" / "lib.png").write_bytes(b"nope")
    return tmp_path


def test_is_image():
    assert _is_image(Path("a.PNG"))
    assert _is_image(Path("b.jpeg"))
    assert not _is_image(Path("c.txt"))


def test_exact_path_match(tree: Path):
    out = _find_files(tree, "docs/arch.png")
    assert [p.name for p in out] == ["arch.png"]


def test_glob_matches_all_png_excluding_pruned_dirs(tree: Path):
    out = _find_files(tree, "*.png")
    names = sorted(p.name for p in out)
    assert names == ["arch.png", "diagram.png"]  # node_modules/lib.png pruned


def test_substring_search(tree: Path):
    out = _find_files(tree, "arch")
    assert [p.name for p in out] == ["arch.png"]


def test_substring_case_insensitive(tree: Path):
    out = _find_files(tree, "README")
    assert [p.name for p in out] == ["readme.md"]


def test_no_match_returns_empty(tree: Path):
    assert _find_files(tree, "*.zip") == []


def test_walk_filtered_prunes_excluded(tree: Path):
    files = {p.name for p in _walk_filtered(tree, 5)}
    assert "lib.png" not in files  # node_modules pruned
    assert "arch.png" in files


def test_walk_filtered_depth_limit(tmp_path: Path):
    deep = tmp_path / "a" / "b" / "c"
    deep.mkdir(parents=True)
    (deep / "deep.txt").write_text("x")
    (tmp_path / "top.txt").write_text("y")
    # depth 1 = only top-level files
    files = {p.name for p in _walk_filtered(tmp_path, 1)}
    assert "top.txt" in files
    assert "deep.txt" not in files


def test_safe_relative(tmp_path: Path):
    f = tmp_path / "docs" / "x.png"
    f.parent.mkdir()
    f.write_bytes(b"i")
    assert _safe_relative(f, tmp_path) == "docs/x.png"
