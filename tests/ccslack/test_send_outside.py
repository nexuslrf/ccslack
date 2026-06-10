from pathlib import Path

from ccslack.handlers.send import _build_browser_view, _within
from ccslack.handlers.send_security import is_path_contained, validate_sendable


def _action_ids(blocks: list[dict]) -> list[str]:
    ids: list[str] = []
    for block in blocks:
        if block.get("type") == "actions":
            ids.extend(e["action_id"] for e in block["elements"])
    return ids


def _values(blocks: list[dict]) -> list[str]:
    out: list[str] = []
    for block in blocks:
        if block.get("type") == "actions":
            out.extend(e["value"] for e in block["elements"])
    return out


# --- containment: lexical, symlink-aware ----------------------------------


def test_symlink_dir_under_cwd_is_contained(tmp_path: Path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "data.txt").write_text("x")
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / "data").symlink_to(outside)

    f = proj / "data" / "data.txt"
    assert is_path_contained(f, proj) is True
    assert validate_sendable(f, proj) is None  # reachable via the symlink


def test_real_outside_path_not_contained(tmp_path: Path):
    outside = tmp_path / "outside"
    outside.mkdir()
    f = outside / "data.txt"
    f.write_text("x")
    proj = tmp_path / "proj"
    proj.mkdir()

    assert is_path_contained(f, proj) is False
    assert validate_sendable(f, proj) == "File is outside project directory"


def test_dotdot_traversal_still_blocked(tmp_path: Path):
    proj = tmp_path / "proj"
    proj.mkdir()
    (tmp_path / "secret.txt").write_text("s")
    f = proj / ".." / "secret.txt"
    assert is_path_contained(f, proj) is False


# --- allow_outside (meta users) -------------------------------------------


def test_allow_outside_skips_containment(tmp_path: Path):
    outside = tmp_path / "outside"
    outside.mkdir()
    f = outside / "data.txt"
    f.write_text("x")
    proj = tmp_path / "proj"
    proj.mkdir()

    assert validate_sendable(f, proj) == "File is outside project directory"
    assert validate_sendable(f, proj, allow_outside=True) is None


def test_allow_outside_still_blocks_secret_pattern(tmp_path: Path):
    outside = tmp_path / "outside"
    outside.mkdir()
    key = outside / "id_rsa.pem"
    key.write_text("-----BEGIN-----")
    proj = tmp_path / "proj"
    proj.mkdir()

    assert validate_sendable(key, proj, allow_outside=True) is not None


# --- browser: symlink navigation + allow_outside roots --------------------


def test_browser_lists_symlinked_dir_under_cwd(tmp_path: Path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "f.txt").write_text("x")
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / "link").symlink_to(outside)

    blocks, _ = _build_browser_view(proj, proj)
    assert str(proj / "link") in _values(blocks)


def test_browser_navigates_into_symlinked_dir(tmp_path: Path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "f.txt").write_text("x")
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / "link").symlink_to(outside)

    blocks, _ = _build_browser_view(proj / "link", proj)
    # File inside the symlinked dir is pickable; its path stays under the cwd.
    assert str(proj / "link" / "f.txt") in _values(blocks)


def test_browser_restricted_cannot_leave_cwd(tmp_path: Path):
    proj = tmp_path / "proj"
    proj.mkdir()
    # Target above the cwd resets to the cwd root → no up button.
    blocks, _ = _build_browser_view(tmp_path, proj)
    assert "ccslack_send_browse:up" not in _action_ids(blocks)


def test_browser_allow_outside_can_leave_cwd(tmp_path: Path):
    proj = tmp_path / "proj"
    proj.mkdir()
    (tmp_path / "sibling.txt").write_text("x")

    blocks, _ = _build_browser_view(tmp_path, proj, allow_outside=True)
    assert "ccslack_send_browse:up" in _action_ids(blocks)  # above cwd, not root
    assert str(tmp_path / "sibling.txt") in _values(blocks)


def test_within_is_lexical(tmp_path: Path):
    proj = tmp_path / "proj"
    proj.mkdir()
    assert _within(proj / "sub", proj) is True
    assert _within(proj, proj) is True
    assert _within(tmp_path, proj) is False
