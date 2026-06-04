import subprocess
from pathlib import Path

import pytest

from ccslack.handlers.send import (
    _CONFIRM_THRESHOLD_BYTES,
    _find_files,
    _human_size,
    _is_image,
    _safe_relative,
    _walk_filtered,
)
from ccslack.handlers.send_security import validate_sendable


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


# --- gitignore is no longer a deny rule -----------------------------------


def test_gitignored_file_is_sendable(tmp_path: Path):
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    (tmp_path / ".gitignore").write_text("*.log\nbuild/\n")
    log = tmp_path / "app.log"
    log.write_text("noise")
    (tmp_path / "build").mkdir()
    artifact = tmp_path / "build" / "out.bin"
    artifact.write_bytes(b"x")
    # Both are gitignored — they must NOT be refused anymore.
    assert validate_sendable(log, tmp_path) is None
    assert validate_sendable(artifact, tmp_path) is None


def test_secret_named_file_still_refused(tmp_path: Path):
    # Removing the gitignore rule must not weaken secret protection.
    key = tmp_path / "id_rsa.pem"
    key.write_text("-----BEGIN-----")
    assert validate_sendable(key, tmp_path) is not None


def test_hidden_file_still_refused(tmp_path: Path):
    env = tmp_path / ".env"
    env.write_text("SECRET=1")
    assert validate_sendable(env, tmp_path) is not None


# --- size confirm threshold -----------------------------------------------


def test_confirm_threshold_is_10mb():
    assert _CONFIRM_THRESHOLD_BYTES == 10 * 1024 * 1024


def test_human_size_formats():
    assert _human_size(500) == "0 KB" or _human_size(500).endswith("KB")
    assert _human_size(2 * 1024 * 1024) == "2.0 MB"
    assert _human_size(12 * 1024 * 1024) == "12.0 MB"
