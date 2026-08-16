"""Tests for the Pi provider: transcript parsing, discovery, and threading hooks."""

import json
import os
import time
from pathlib import Path

from ccslack.providers.pi import PiProvider, encode_cwd_dirname
from ccslack.providers.pi_format import parse_assistant, normalize_pending


def _write_session(
    home: Path,
    *,
    session_id: str,
    cwd: str,
    age: float = 0.0,
    entries: list[dict] | None = None,
) -> Path:
    session_dir = home / ".pi" / "agent" / "sessions" / encode_cwd_dirname(cwd)
    session_dir.mkdir(parents=True, exist_ok=True)
    fpath = session_dir / f"2026-08-15T20-00-00-000Z_{session_id}.jsonl"
    lines = [
        json.dumps({"type": "session", "version": 3, "id": session_id, "cwd": cwd})
    ]
    if entries:
        lines.extend(json.dumps(e) for e in entries)
    fpath.write_text("\n".join(lines) + "\n")
    if age:
        stamp = time.time() - age
        os.utime(fpath, (stamp, stamp))
    return fpath


def test_caps_enable_hook_and_incremental_read():
    caps = PiProvider().capabilities
    assert caps.name == "pi"
    assert caps.supports_hook is True
    assert caps.supports_hook_events is True
    assert caps.supports_incremental_read is True
    assert caps.supports_resume is True
    assert caps.transcript_format == "jsonl"


def test_encode_cwd_dirname_round_trips():
    assert encode_cwd_dirname("/home/ruofanl/ccslack") == "--home-ruofanl-ccslack--"
    assert encode_cwd_dirname("/") == "----"
    assert encode_cwd_dirname("/tmp/foo/") == "--tmp-foo--"


def test_make_launch_args_prefers_session():
    p = PiProvider()
    assert p.make_launch_args() == ""
    assert p.make_launch_args(use_continue=True) == "--continue"
    # shlex.quote wraps paths containing spaces
    assert "--session" in p.make_launch_args(resume_id="/path with space/s.jsonl")


def test_parse_transcript_line_unwraps_message_envelope():
    p = PiProvider()
    line = json.dumps(
        {
            "type": "message",
            "id": "a1",
            "timestamp": "t",
            "message": {"role": "user", "content": "hi"},
        }
    )
    flat = p.parse_transcript_line(line)
    assert flat is not None
    assert flat["type"] == "user"
    assert flat["message"]["role"] == "user"


def test_parse_transcript_line_passes_through_non_message_entries():
    p = PiProvider()
    line = json.dumps({"type": "session", "version": 3, "id": "uuid", "cwd": "/p"})
    flat = p.parse_transcript_line(line)
    assert flat is not None
    assert flat["type"] == "session"


def test_parse_assistant_emits_thinking_and_classifies_phase():
    pending = normalize_pending({})
    msg = {
        "role": "assistant",
        "stopReason": "toolUse",
        "content": [
            {
                "type": "thinking",
                "thinking": "I should read the file first to understand it.",
            },
            {"type": "text", "text": "Let me look at the provider."},
            {
                "type": "toolCall",
                "id": "call_1",
                "name": "read",
                "arguments": {"path": "src/x.py"},
            },
        ],
    }
    out, pending = parse_assistant(msg, pending)
    kinds = [(m.content_type, m.phase) for m in out]
    assert ("thinking", None) in kinds
    assert ("text", "commentary") in kinds
    assert ("tool_use", None) in kinds
    assert pending["call_1"][1] == "Read"


def test_parse_assistant_final_answer_phase_on_terminal_stop():
    msg = {
        "role": "assistant",
        "stopReason": "stop",
        "content": [{"type": "text", "text": "Done, all fixed."}],
    }
    out, _ = parse_assistant(msg, normalize_pending({}))
    assert out[0].content_type == "text"
    assert out[0].phase == "final_answer"


def test_parse_assistant_thinking_only_turn_surfaces_placeholder():
    msg = {
        "role": "assistant",
        "stopReason": "stop",
        "content": [{"type": "thinking", "thinking": ""}],
    }
    out, _ = parse_assistant(msg, normalize_pending({}))
    assert len(out) == 1
    assert out[0].content_type == "thinking"
    assert out[0].text == "(thinking)"


def test_parse_assistant_error_notice_appended():
    msg = {
        "role": "assistant",
        "stopReason": "error",
        "errorMessage": "rate limited",
        "content": [{"type": "text", "text": "partial"}],
    }
    out, _ = parse_assistant(msg, normalize_pending({}))
    assert any("API error" in m.text for m in out)


def test_parse_transcript_entries_pairs_tool_result():
    p = PiProvider()
    entries = [
        {
            "type": "assistant",
            "message": {
                "role": "assistant",
                "stopReason": "toolUse",
                "content": [
                    {
                        "type": "toolCall",
                        "id": "c1",
                        "name": "bash",
                        "arguments": {"command": "ls"},
                    }
                ],
            },
        },
        {
            "type": "toolResult",
            "message": {
                "role": "toolResult",
                "toolCallId": "c1",
                "toolName": "bash",
                "content": [{"type": "text", "text": "file.txt"}],
                "isError": False,
            },
        },
    ]
    messages, pending = p.parse_transcript_entries(entries, pending_tools={})
    assert messages[0].content_type == "tool_use"
    assert messages[0].tool_name == "Bash"
    assert messages[1].content_type == "tool_result"
    assert messages[1].tool_name == "Bash"
    assert pending == {}


def test_discover_transcript_matches_cwd(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    proj = tmp_path / "proj"
    proj.mkdir()
    fpath = _write_session(tmp_path, session_id="01a-uuid", cwd=str(proj))
    event = PiProvider().discover_transcript(str(proj), "ccslack:@1")
    assert event is not None
    assert event.session_id == "01a-uuid"
    assert event.transcript_path == str(fpath)
    assert event.window_key == "ccslack:@1"


def test_discover_transcript_none_when_cwd_differs(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    proj = tmp_path / "proj"
    proj.mkdir()
    _write_session(tmp_path, session_id="01a-uuid", cwd=str(tmp_path / "other"))
    assert PiProvider().discover_transcript(str(proj), "ccslack:@1") is None


def test_discover_transcript_respects_max_age(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    proj = tmp_path / "proj"
    proj.mkdir()
    _write_session(tmp_path, session_id="old-1", cwd=str(proj), age=300.0)
    assert (
        PiProvider().discover_transcript(str(proj), "ccslack:@1", max_age=120.0) is None
    )


def test_discover_transcript_picks_newest_match(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    proj = tmp_path / "proj"
    proj.mkdir()
    _write_session(tmp_path, session_id="older", cwd=str(proj), age=10.0)
    newer = _write_session(tmp_path, session_id="newer", cwd=str(proj), age=1.0)
    event = PiProvider().discover_transcript(str(proj), "ccslack:@1")
    assert event is not None
    assert event.session_id == "newer"
    assert event.transcript_path == str(newer)
