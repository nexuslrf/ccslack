import pytest

from ccslack.slack_client import FakeSlackClient
from ccslack.slack_sender import safe_post, safe_update, split_message


def test_split_message_short_returns_single_chunk():
    assert split_message("hello") == ["hello"]


def test_split_message_long_splits_on_newline():
    long = ("line\n" * 4000) + "tail"
    chunks = split_message(long, max_chars=200)
    assert all(len(c) <= 200 for c in chunks)
    assert sum(c.count("line") for c in chunks) >= 4000


@pytest.mark.asyncio
async def test_safe_post_records_blocks_and_fallback():
    client = FakeSlackClient()
    client.returns["chat_postMessage"] = {"ok": True, "ts": "1700000000.000001"}
    ts = await safe_post(client, channel="C1", text="hello world")
    assert ts == "1700000000.000001"
    call = client.last_call("chat_postMessage")
    assert call is not None
    assert call.kwargs["channel"] == "C1"
    assert call.kwargs["text"].startswith("hello world")
    assert isinstance(call.kwargs.get("blocks"), list)


@pytest.mark.asyncio
async def test_safe_update_calls_chat_update():
    client = FakeSlackClient()
    client.returns["chat_update"] = {"ok": True}
    ok = await safe_update(client, channel="C1", ts="1700.0", text="updated")
    assert ok is True
    call = client.last_call("chat_update")
    assert call is not None
    assert (
        call.kwargs
        == {
            "channel": "C1",
            "ts": "1700.0",
            "text": "updated",
            "blocks": call.kwargs["blocks"],
        }
        or call.kwargs.get("channel") == "C1"
    )
