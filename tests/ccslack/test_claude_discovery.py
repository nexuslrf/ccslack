"""Tests for Claude hookless discovery (in-TUI branch/fork fallback)."""

import json
import os
import time
from pathlib import Path

from ccslack.providers.claude import ClaudeProvider


def _write_claude_session(
    home: Path,
    session_id: str,
    cwd: str,
    *,
    age: float = 0.0,
) -> Path:
    project_dir = home / ".claude" / "projects" / cwd.replace("/", "-")
    project_dir.mkdir(parents=True, exist_ok=True)
    fpath = project_dir / f"{session_id}.jsonl"
    fpath.write_text(
        json.dumps({"type": "attachment", "sessionId": session_id, "cwd": cwd})
        + "\n"
    )
    if age:
        stamp = time.time() - age
        os.utime(fpath, (stamp, stamp))
    return fpath


def test_caps_enable_hookless_discovery():
    c = ClaudeProvider()
    assert c.capabilities.supports_hook is True
    assert c.capabilities.supports_hookless_discovery is True


def test_discover_transcript_finds_newest(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    proj = tmp_path / "proj"
    proj.mkdir()
    _write_claude_session(tmp_path, "019f1234-aaaa-bbbb-cccc-012345678900", str(proj), age=10.0)
    newer = _write_claude_session(tmp_path, "019f1234-aaaa-bbbb-cccc-012345678901", str(proj), age=1.0)
    event = ClaudeProvider().discover_transcript(str(proj), "ccslack:@1")
    assert event is not None
    assert event.session_id == "019f1234-aaaa-bbbb-cccc-012345678901"
    assert event.transcript_path == str(newer)


def test_discover_transcript_none_when_cwd_differs(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    proj = tmp_path / "proj"
    proj.mkdir()
    _write_claude_session(tmp_path, "019f1234-aaaa-bbbb-cccc-012345678902", str(tmp_path / "other"))
    assert ClaudeProvider().discover_transcript(str(proj), "ccslack:@1") is None


def test_discover_transcript_respects_max_age(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    proj = tmp_path / "proj"
    proj.mkdir()
    _write_claude_session(tmp_path, "019f1234-aaaa-bbbb-cccc-012345678900", str(proj), age=300.0)
    assert (
        ClaudeProvider().discover_transcript(str(proj), "ccslack:@1", max_age=120.0)
        is None
    )


def test_resolve_session_transcript_finds_by_id(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    proj = tmp_path / "proj"
    proj.mkdir()
    fpath = _write_claude_session(
        tmp_path, "019f1234-aaaa-bbbb-cccc-012345678903", str(proj), age=600.0
    )
    assert (
        ClaudeProvider().resolve_session_transcript("019f1234-aaaa-bbbb-cccc-012345678903", str(proj))
        == str(fpath)
    )


def test_resolve_session_transcript_none_for_unknown_id(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    proj = tmp_path / "proj"
    proj.mkdir()
    _write_claude_session(tmp_path, "019f1234-aaaa-bbbb-cccc-012345678904", str(proj))
    assert (
        ClaudeProvider().resolve_session_transcript("nope-uuid", str(proj)) is None
    )


def test_resolve_session_transcript_rejects_non_uuid(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    p = ClaudeProvider()
    assert p.resolve_session_transcript("not-a-uuid", "/tmp") is None
    assert p.resolve_session_transcript("", "/tmp") is None


def test_is_transcript_stale_detects_frozen_file(tmp_path):
    from ccslack.session_monitor import _is_transcript_stale

    fpath = tmp_path / "stale.jsonl"
    fpath.write_text("{}\n")
    # Set mtime to 60s ago — stale
    old = time.time() - 60
    os.utime(fpath, (old, old))
    assert _is_transcript_stale(str(fpath)) is True


def test_is_transcript_stale_false_for_recent_file(tmp_path):
    from ccslack.session_monitor import _is_transcript_stale

    fpath = tmp_path / "fresh.jsonl"
    fpath.write_text("{}\n")
    # Just written — fresh
    assert _is_transcript_stale(str(fpath)) is False


def test_is_transcript_stale_true_for_missing_file():
    from ccslack.session_monitor import _is_transcript_stale

    assert _is_transcript_stale("/nonexistent/path.jsonl") is True


def test_is_transcript_stale_true_for_empty_path():
    from ccslack.session_monitor import _is_transcript_stale

    assert _is_transcript_stale("") is True


def test_is_transcript_fresher_rejects_stale_candidate(tmp_path):
    from ccslack.session_monitor import _is_transcript_fresher

    tracked = tmp_path / "tracked.jsonl"
    candidate = tmp_path / "candidate.jsonl"
    tracked.write_text("{}\n")
    candidate.write_text("{}\n")
    # Both stale → candidate not fresher
    old = time.time() - 60
    os.utime(tracked, (old, old))
    os.utime(candidate, (old, old))
    assert _is_transcript_fresher(str(candidate), str(tracked)) is False


def test_is_transcript_fresher_accepts_active_newer(tmp_path):
    from ccslack.session_monitor import _is_transcript_fresher

    tracked = tmp_path / "tracked.jsonl"
    candidate = tmp_path / "candidate.jsonl"
    tracked.write_text("{}\n")
    candidate.write_text("{}\n")
    # Tracked stale (60s ago), candidate fresh (now)
    old = time.time() - 60
    os.utime(tracked, (old, old))
    assert _is_transcript_fresher(str(candidate), str(tracked)) is True


def test_is_transcript_fresher_rejects_older_candidate(tmp_path):
    from ccslack.session_monitor import _is_transcript_fresher

    tracked = tmp_path / "tracked.jsonl"
    candidate = tmp_path / "candidate.jsonl"
    tracked.write_text("{}\n")
    candidate.write_text("{}\n")
    # Both fresh, but candidate is older than tracked → not fresher
    now = time.time()
    os.utime(tracked, (now, now))
    os.utime(candidate, (now - 5, now - 5))
    assert _is_transcript_fresher(str(candidate), str(tracked)) is False


def test_is_transcript_fresher_handles_missing_files(tmp_path):
    from ccslack.session_monitor import _is_transcript_fresher

    tracked = tmp_path / "tracked.jsonl"
    tracked.write_text("{}\n")
    # Candidate missing → False
    assert _is_transcript_fresher(str(tmp_path / "nope.jsonl"), str(tracked)) is False
    # Tracked missing, candidate fresh → True
    candidate = tmp_path / "candidate.jsonl"
    candidate.write_text("{}\n")
    assert _is_transcript_fresher(str(candidate), str(tmp_path / "nope.jsonl")) is True
