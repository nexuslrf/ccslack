import time

import pytest

from ccslack.config import config
from ccslack.handlers import purge
from ccslack.handlers.meta import _handle_autopurge, _handle_purge, _parse_duration
from ccslack.slack_client import FakeSlackClient
from ccslack.thread_router import thread_router


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "purge_file", tmp_path / "purge.json")
    purge._loaded = True  # skip disk load
    purge.reset_for_testing()
    thread_router.reset()
    yield
    purge.reset_for_testing()
    thread_router.reset()


def _deleted_ts(client: FakeSlackClient) -> list[str]:
    return [c.kwargs["ts"] for c in client.calls if c.method == "chat_delete"]


@pytest.mark.asyncio
async def test_purge_all_deletes_everything_recorded():
    purge.record("C1", "1.1", kind="answer")
    purge.record("C1", "1.2", kind="tool", thread_ts="1.0")
    client = FakeSlackClient()

    n = await purge.purge(client, "C1")

    assert n == 2
    assert set(_deleted_ts(client)) == {"1.1", "1.2"}
    # ledger emptied
    assert await purge.purge(client, "C1") == 0


@pytest.mark.asyncio
async def test_purge_last_n():
    for i in range(5):
        purge.record("C1", f"{i}.0")
    client = FakeSlackClient()

    n = await purge.purge(client, "C1", count=2)

    assert n == 2
    assert _deleted_ts(client) == ["3.0", "4.0"]


@pytest.mark.asyncio
async def test_purge_round_only_answers_of_that_round():
    purge.bump_round("C1")  # round 1
    purge.record("C1", "r1.ans", kind="answer")
    purge.record("C1", "r1.ctl", kind="control")
    purge.record("C1", "r1.tool", kind="tool", thread_ts="r1.parent")
    purge.bump_round("C1")  # round 2
    purge.record("C1", "r2.ans", kind="answer")
    client = FakeSlackClient()

    n = await purge.purge_round(client, "C1", 1)

    assert set(_deleted_ts(client)) == {"r1.ans", "r1.ctl"}  # not the tool, not r2
    assert n == 2


@pytest.mark.asyncio
async def test_purge_thread_deletes_parent_and_children():
    purge.record("C1", "p", kind="thread_parent", thread_ts="p")
    purge.record("C1", "c1", kind="tool", thread_ts="p")
    purge.record("C1", "c2", kind="thinking", thread_ts="p")
    purge.record("C1", "other", kind="answer")
    client = FakeSlackClient()

    n = await purge.purge_thread(client, "C1", "p")

    assert set(_deleted_ts(client)) == {"p", "c1", "c2"}
    assert n == 3
    # the unrelated answer survives
    assert await purge.purge(client, "C1") == 1


@pytest.mark.asyncio
async def test_autopurge_sweep_deletes_old_only():
    now = time.time()
    purge.set_autopurge("C1", 1.0)  # 1 hour
    purge.record("C1", f"{now - 7200:.4f}")  # 2h old → stale
    purge.record("C1", f"{now - 60:.4f}")  # 1m old → keep
    client = FakeSlackClient()

    n = await purge.sweep(client)

    assert n == 1
    assert len(_deleted_ts(client)) == 1


def test_autopurge_get_set_off():
    assert purge.get_autopurge("C1") == 0.0
    purge.set_autopurge("C1", 2.5)
    assert purge.get_autopurge("C1") == 2.5
    purge.set_autopurge("C1", None)
    assert purge.get_autopurge("C1") == 0.0


@pytest.mark.asyncio
async def test_ledger_and_autopurge_survive_reload():
    # Simulate a bot restart: record, persist, drop in-memory state, reload.
    purge.bump_round("C1")
    purge.bump_round("C1")  # round 2
    purge.record("C1", "1.1", kind="answer")
    purge.set_autopurge("C1", 2.0)
    # Drop in-memory state but keep the file, then force a fresh load.
    purge._ledger.clear()
    purge._autopurge.clear()
    purge._round.clear()
    purge._loaded = False
    purge._ensure_loaded()

    assert purge.get_autopurge("C1") == 2.0
    # Round counter is seeded past the persisted max so new rounds don't collide.
    purge.bump_round("C1")
    assert purge.current_round("C1") > 2
    client = FakeSlackClient()
    assert await purge.purge(client, "C1") == 1  # the reloaded entry


def test_file_id_from_upload_shapes():
    assert purge.file_id_from_upload({"files": [{"id": "F1"}]}) == "F1"
    assert purge.file_id_from_upload({"file": {"id": "F2"}}) == "F2"
    assert purge.file_id_from_upload({"ok": True}) == ""
    assert purge.file_id_from_upload(None) == ""


def _files_deleted(client: FakeSlackClient) -> list[str]:
    return [c.kwargs["file"] for c in client.calls if c.method == "files_delete"]


@pytest.mark.asyncio
async def test_file_close_button_records_and_delete_file_removes_both():
    client = FakeSlackClient()
    client.returns["chat_postMessage"] = {"ok": True, "ts": "btn.ts"}

    await purge.post_file_close_button(client, "C1", "F123")

    # Clicking Remove deletes the file AND the button message.
    await purge.delete_file(client, "C1", "F123", "btn.ts")
    assert _files_deleted(client) == ["F123"]
    assert "btn.ts" in _deleted_ts(client)


@pytest.mark.asyncio
async def test_purge_all_also_deletes_recorded_files():
    purge.record("C1", "btn.ts", kind="file", file_id="F9")
    purge.record("C1", "ans.ts", kind="answer")
    client = FakeSlackClient()

    await purge.purge(client, "C1")

    assert _files_deleted(client) == ["F9"]
    assert set(_deleted_ts(client)) == {"btn.ts", "ans.ts"}


@pytest.mark.asyncio
async def test_forget_channel_clears_state():
    purge.record("C1", "1.1")
    purge.set_autopurge("C1", 1.0)
    purge.forget_channel("C1")
    client = FakeSlackClient()
    assert await purge.purge(client, "C1") == 0
    assert purge.get_autopurge("C1") == 0.0


# --- duration parsing + commands -------------------------------------------


def test_parse_duration():
    assert _parse_duration("30m") == 1800.0
    assert _parse_duration("1.5h") == 5400.0
    assert _parse_duration("2") == 7200.0  # default hours
    assert _parse_duration("2d") == 172800.0
    assert _parse_duration("45s") == 45.0
    assert _parse_duration("nope") is None


@pytest.mark.asyncio
async def test_handle_purge_all(monkeypatch):
    thread_router.bind_channel("C1", "@1")
    purge.record("C1", "1.1")
    purge.record("C1", "1.2")
    client = FakeSlackClient()

    await _handle_purge(client, "C1", "U1", ["all"])

    assert client.call_count("chat_delete") == 2
    eph = client.last_call("chat_postEphemeral")
    assert "Purged 2" in eph.kwargs["text"]


@pytest.mark.asyncio
async def test_handle_purge_last_n(monkeypatch):
    thread_router.bind_channel("C1", "@1")
    for i in range(4):
        purge.record("C1", f"{i}.0")
    client = FakeSlackClient()

    await _handle_purge(client, "C1", "U1", ["2"])

    assert client.call_count("chat_delete") == 2


@pytest.mark.asyncio
async def test_handle_purge_rejects_unbound():
    client = FakeSlackClient()
    await _handle_purge(client, "C0", "U1", ["all"])
    assert client.call_count("chat_delete") == 0
    eph = client.last_call("chat_postEphemeral")
    assert "bound session channel" in eph.kwargs["text"]


@pytest.mark.asyncio
async def test_handle_autopurge_set_and_off():
    thread_router.bind_channel("C1", "@1")
    client = FakeSlackClient()

    await _handle_autopurge(client, "C1", "U1", ["1.5h"])
    assert purge.get_autopurge("C1") == 1.5

    await _handle_autopurge(client, "C1", "U1", ["off"])
    assert purge.get_autopurge("C1") == 0.0


@pytest.mark.asyncio
async def test_post_response_button_then_round_purge_clears_both():
    purge.bump_round("C1")  # round 1
    purge.record("C1", "ans.ts", kind="answer")
    client = FakeSlackClient()
    client.returns["chat_postMessage"] = {"ok": True, "ts": "btn.ts"}

    await purge.post_response_button(client, "C1")

    msg = client.last_call("chat_postMessage")
    assert "ccslack_purge_response" in str(msg.kwargs["blocks"])
    # Clicking purges the round → the answer AND the button message.
    await purge.purge_round(client, "C1", 1)
    assert set(_deleted_ts(client)) == {"ans.ts", "btn.ts"}


@pytest.mark.asyncio
async def test_response_button_posted_once_per_round():
    purge.bump_round("C1")  # round 1
    client = FakeSlackClient()
    client.set_side_effect(
        "chat_postMessage",
        [{"ok": True, "ts": "b1"}, {"ok": True, "ts": "b2"}],
    )

    # Several output messages in the same round → only ONE button.
    await purge.post_response_button(client, "C1")
    await purge.post_response_button(client, "C1")
    assert client.call_count("chat_postMessage") == 1

    # A new round offers a button again.
    purge.bump_round("C1")
    await purge.post_response_button(client, "C1")
    assert client.call_count("chat_postMessage") == 2


@pytest.mark.asyncio
async def test_purge_round_annotates_echo_without_deleting_it():
    purge.bump_round("C1")  # round 1
    purge.record("C1", "echo.ts", kind="echo", text=":bust_in_silhouette: do X")
    purge.record("C1", "ans.ts", kind="answer")
    client = FakeSlackClient()

    await purge.purge_round(client, "C1", 1)

    # Answer deleted; echo NOT deleted.
    assert "ans.ts" in _deleted_ts(client)
    assert "echo.ts" not in _deleted_ts(client)
    # Echo edited in place to note the purge, preserving the prompt.
    upd = client.last_call("chat_update")
    assert upd is not None
    assert upd.kwargs["ts"] == "echo.ts"
    assert "do X" in str(upd.kwargs)
    assert "purged" in str(upd.kwargs).lower()


@pytest.mark.asyncio
async def test_handle_autopurge_reports_state():
    thread_router.bind_channel("C1", "@1")
    purge.set_autopurge("C1", 3.0)
    client = FakeSlackClient()

    await _handle_autopurge(client, "C1", "U1", [])

    eph = client.last_call("chat_postEphemeral")
    assert "every 3h" in eph.kwargs["text"]
