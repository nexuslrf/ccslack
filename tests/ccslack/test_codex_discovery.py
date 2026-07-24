import json
import os
import time
from pathlib import Path

from ccslack.providers.codex import CodexProvider


def _write_rollout(
    home: Path,
    name: str,
    *,
    session_id: str,
    cwd: str,
    originator: str = "codex_cli_rs",
    source: object = "cli",
    age: float = 0.0,
) -> Path:
    day_dir = home / ".codex" / "sessions" / "2026" / "07" / "20"
    day_dir.mkdir(parents=True, exist_ok=True)
    fpath = day_dir / f"rollout-2026-07-20T20-00-00-{name}.jsonl"
    meta = {
        "type": "session_meta",
        "payload": {
            "id": session_id,
            "session_id": session_id,
            "cwd": cwd,
            "originator": originator,
            "source": source,
        },
    }
    fpath.write_text(json.dumps(meta) + "\n")
    if age:
        stamp = time.time() - age
        os.utime(fpath, (stamp, stamp))
    return fpath


def test_caps_enable_hookless_discovery():
    assert CodexProvider().capabilities.supports_hookless_discovery is True


def test_caps_keep_hook_as_fast_path():
    assert CodexProvider().capabilities.supports_hook is True


def test_discover_transcript_matches_cwd(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    fpath = _write_rollout(
        tmp_path, "abc", session_id="019f-abc", cwd=str(tmp_path / "proj")
    )
    (tmp_path / "proj").mkdir()
    event = CodexProvider().discover_transcript(str(tmp_path / "proj"), "ccslack:@1")
    assert event is not None
    assert event.session_id == "019f-abc"
    assert event.transcript_path == str(fpath)
    assert event.window_key == "ccslack:@1"


def test_discover_transcript_none_when_cwd_differs(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    _write_rollout(tmp_path, "abc", session_id="019f-abc", cwd=str(tmp_path / "other"))
    (tmp_path / "proj").mkdir()
    assert (
        CodexProvider().discover_transcript(str(tmp_path / "proj"), "ccslack:@1")
        is None
    )


def test_discover_transcript_skips_codex_exec_and_picks_primary(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    proj = tmp_path / "proj"
    proj.mkdir()
    _write_rollout(
        tmp_path,
        "exec",
        session_id="exec-1",
        cwd=str(proj),
        originator="codex_exec",
        age=1.0,
    )
    primary = _write_rollout(
        tmp_path, "prim", session_id="prim-1", cwd=str(proj), age=5.0
    )
    event = CodexProvider().discover_transcript(str(proj), "ccslack:@1")
    assert event is not None
    assert event.session_id == "prim-1"
    assert event.transcript_path == str(primary)


def test_discover_transcript_respects_max_age(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    proj = tmp_path / "proj"
    proj.mkdir()
    _write_rollout(tmp_path, "old", session_id="old-1", cwd=str(proj), age=300.0)
    assert (
        CodexProvider().discover_transcript(str(proj), "ccslack:@1", max_age=120.0)
        is None
    )
