import json
import os
from pathlib import Path
import sqlite3

import pytest

from ccslack.providers import (
    detect_provider_from_command,
    detect_provider_from_transcript_path,
)
from ccslack.providers import cursor as cursor_mod
from ccslack.providers.cursor import CursorProvider


def _make_store(path: Path, messages: list) -> None:
    con = sqlite3.connect(str(path))
    con.execute("CREATE TABLE blobs (id TEXT PRIMARY KEY, data BLOB)")
    con.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT)")
    for i, msg in enumerate(messages):
        data = msg if isinstance(msg, bytes) else json.dumps(msg).encode()
        con.execute("INSERT INTO blobs (id, data) VALUES (?, ?)", (f"id{i}", data))
    con.commit()
    con.close()


_CONVERSATION = [
    {"role": "system", "content": "you are cursor"},
    {"role": "user", "content": [{"type": "text", "text": "<user_query>hi</user_query>"}]},
    {
        "role": "assistant",
        "content": [
            {"type": "redacted-reasoning", "data": "xxxx"},
            {"type": "text", "text": "Doing it."},
            {
                "type": "tool-call",
                "toolCallId": "tc1",
                "toolName": "Shell",
                "args": {"command": "ls -la"},
            },
        ],
    },
    b"\x12\x9c\x06\x99\x06binary-protobuf-dag-node",
    {
        "role": "tool",
        "content": [
            {
                "type": "tool-result",
                "toolCallId": "tc1",
                "toolName": "Shell",
                "result": "file1\nfile2",
            }
        ],
    },
    {"role": "assistant", "content": [{"type": "text", "text": "Done."}]},
]


def test_make_launch_args():
    p = CursorProvider()
    assert p.make_launch_args() == ""
    assert p.make_launch_args(use_continue=True) == "--continue"
    assert p.make_launch_args("8782f8a7-6254-43d2-9a1f-c32bf9257a44") == (
        "--resume 8782f8a7-6254-43d2-9a1f-c32bf9257a44"
    )


def test_make_launch_args_rejects_shell_metachars():
    with pytest.raises(ValueError):
        CursorProvider().make_launch_args("abc; rm -rf /")


def test_read_transcript_file_skips_binary_and_advances_offset(tmp_path):
    store = tmp_path / "store.db"
    _make_store(store, _CONVERSATION)
    entries, offset = CursorProvider().read_transcript_file(str(store), 0)
    # 5 JSON messages kept, binary DAG node skipped, offset = last rowid (6).
    assert len(entries) == 5
    assert offset == 6
    assert [e["role"] for e in entries] == [
        "system",
        "user",
        "assistant",
        "tool",
        "assistant",
    ]


def test_read_transcript_file_incremental(tmp_path):
    store = tmp_path / "store.db"
    _make_store(store, _CONVERSATION)
    entries, offset = CursorProvider().read_transcript_file(str(store), 3)
    # rowids 4 (binary, skipped), 5 (tool), 6 (assistant)
    assert [e["role"] for e in entries] == ["tool", "assistant"]
    assert offset == 6


def test_read_transcript_file_strips_wal_suffix(tmp_path):
    store = tmp_path / "store.db"
    _make_store(store, _CONVERSATION)
    entries, _ = CursorProvider().read_transcript_file(str(store) + "-wal", 0)
    assert len(entries) == 5


def test_read_transcript_file_missing_db_is_graceful(tmp_path):
    entries, offset = CursorProvider().read_transcript_file(
        str(tmp_path / "nope.db"), 7
    )
    assert entries == []
    assert offset == 7


def test_parse_transcript_entries_emits_text_and_tool_pairing(tmp_path):
    store = tmp_path / "store.db"
    _make_store(store, _CONVERSATION)
    p = CursorProvider()
    entries, _ = p.read_transcript_file(str(store), 0)
    messages, pending = p.parse_transcript_entries(entries, {})

    kinds = [(m.content_type, m.text) for m in messages]
    assert ("text", "Doing it.") in kinds
    assert ("text", "Done.") in kinds
    tool_use = next(m for m in messages if m.content_type == "tool_use")
    tool_result = next(m for m in messages if m.content_type == "tool_result")
    assert tool_use.tool_use_id == "tc1"
    assert "Shell" in tool_use.text and "ls -la" in tool_use.text
    assert tool_result.tool_use_id == "tc1"
    assert tool_result.text == "file1\nfile2"
    # user + system turns are not echoed back; tool call was resolved.
    assert all(m.role == "assistant" for m in messages)
    assert pending == {}


def test_discover_transcript_matches_cwd_hash(tmp_path, monkeypatch):
    chats_root = tmp_path / "chats"
    monkeypatch.setattr(cursor_mod, "_cursor_chats_root", lambda: chats_root)

    cwd = tmp_path / "proj"
    cwd.mkdir()
    project_hash = cursor_mod.project_hash(str(cwd))
    agent_dir = chats_root / project_hash / "agent-xyz"
    agent_dir.mkdir(parents=True)
    _make_store(agent_dir / "store.db", _CONVERSATION)

    event = CursorProvider().discover_transcript(str(cwd), "ccslack:@1")
    assert event is not None
    assert event.session_id == "agent-xyz"
    assert event.cwd == str(cwd.resolve())
    assert event.transcript_path.endswith("store.db")
    assert event.window_key == "ccslack:@1"


def test_discover_transcript_prefers_wal_path(tmp_path, monkeypatch):
    chats_root = tmp_path / "chats"
    monkeypatch.setattr(cursor_mod, "_cursor_chats_root", lambda: chats_root)
    cwd = tmp_path / "proj"
    cwd.mkdir()
    agent_dir = chats_root / cursor_mod.project_hash(str(cwd)) / "agent-1"
    agent_dir.mkdir(parents=True)
    _make_store(agent_dir / "store.db", _CONVERSATION)
    (agent_dir / "store.db-wal").write_bytes(b"wal")

    event = CursorProvider().discover_transcript(str(cwd), "ccslack:@1")
    assert event is not None
    assert event.transcript_path.endswith("store.db-wal")


def test_discover_transcript_none_when_no_chat(tmp_path, monkeypatch):
    monkeypatch.setattr(cursor_mod, "_cursor_chats_root", lambda: tmp_path / "chats")
    assert CursorProvider().discover_transcript(str(tmp_path), "ccslack:@1") is None


def test_discover_transcript_respects_max_age(tmp_path, monkeypatch):
    chats_root = tmp_path / "chats"
    monkeypatch.setattr(cursor_mod, "_cursor_chats_root", lambda: chats_root)
    cwd = tmp_path / "proj"
    cwd.mkdir()
    agent_dir = chats_root / cursor_mod.project_hash(str(cwd)) / "agent-old"
    agent_dir.mkdir(parents=True)
    store = agent_dir / "store.db"
    _make_store(store, _CONVERSATION)
    old = 1_000_000.0
    os.utime(store, (old, old))

    assert (
        CursorProvider().discover_transcript(str(cwd), "ccslack:@1", max_age=60.0)
        is None
    )


def test_detection_from_command_and_path():
    assert detect_provider_from_command("/home/u/.local/bin/cursor-agent -p") == "cursor"
    assert detect_provider_from_command("cursor-agent") == "cursor"
    assert (
        detect_provider_from_transcript_path("/home/u/.cursor/chats/a/b/store.db-wal")
        == "cursor"
    )
