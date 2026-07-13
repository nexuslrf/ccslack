import pytest

from ccslack.slack_client import FakeSlackClient
from ccslack import slack_sender
from ccslack.slack_sender import (
    MAX_POST_CHARS,
    SLACK_MAX_BLOCKS,
    _cap_blocks,
    safe_post,
    safe_update,
    split_message,
)


@pytest.fixture
def _no_rate_limit(monkeypatch):
    async def _noop(_channel):
        return None

    monkeypatch.setattr(slack_sender, "rate_limit_send", _noop)


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
async def test_safe_post_splits_long_text_into_multiple_messages(_no_rate_limit):
    client = FakeSlackClient()
    client.returns["chat_postMessage"] = {"ok": True, "ts": "1700.1"}
    long = ("paragraph line.\n" * 3000)  # ~48k chars, > MAX_POST_CHARS
    assert len(long) > MAX_POST_CHARS

    ts = await safe_post(client, channel="C1", text=long)

    assert ts == "1700.1"  # first part's ts is returned
    assert client.call_count("chat_postMessage") > 1  # spilled into several posts


@pytest.mark.asyncio
async def test_safe_post_short_text_is_single_message(_no_rate_limit):
    client = FakeSlackClient()
    client.returns["chat_postMessage"] = {"ok": True, "ts": "1700.2"}

    await safe_post(client, channel="C1", text="short answer")

    assert client.call_count("chat_postMessage") == 1


@pytest.mark.asyncio
async def test_safe_post_explicit_blocks_never_split(_no_rate_limit):
    client = FakeSlackClient()
    client.returns["chat_postMessage"] = {"ok": True, "ts": "1700.3"}
    blocks = [{"type": "section", "text": {"type": "mrkdwn", "text": "hi"}}]

    await safe_post(client, channel="C1", text="x" * (MAX_POST_CHARS + 5), blocks=blocks)

    assert client.call_count("chat_postMessage") == 1


def test_cap_blocks_bounds_to_slack_limit():
    blocks = [
        {"type": "section", "text": {"type": "mrkdwn", "text": str(i)}}
        for i in range(SLACK_MAX_BLOCKS + 10)
    ]
    capped = _cap_blocks(blocks)
    assert len(capped) == SLACK_MAX_BLOCKS
    assert "truncated" in capped[-1]["text"]["text"]


def test_cap_blocks_passthrough_under_limit():
    blocks = [{"type": "section", "text": {"type": "mrkdwn", "text": "a"}}]
    assert _cap_blocks(blocks) is blocks


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
