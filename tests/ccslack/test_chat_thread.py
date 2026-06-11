import pytest

from ccslack.handlers.meta import _handle_chat
from ccslack.slack_client import FakeSlackClient
from ccslack.thread_router import thread_router


@pytest.fixture(autouse=True)
def _clean():
    thread_router.reset()
    yield
    thread_router.reset()


@pytest.mark.asyncio
async def test_chat_posts_parent_and_marks_thread():
    thread_router.bind_channel("C1", "@1", window_name="proj")
    client = FakeSlackClient()
    client.returns["chat_postMessage"] = {"ok": True, "ts": "123.45"}

    await _handle_chat(client, "C1", "U1", ["let's", "discuss"])

    msg = client.last_call("chat_postMessage")
    assert msg is not None
    assert "Chat thread" in msg.kwargs["text"]
    assert "discuss" in msg.kwargs["text"]
    assert thread_router.is_chat_thread("C1", "123.45") is True


@pytest.mark.asyncio
async def test_chat_no_topic_still_marks_thread():
    thread_router.bind_channel("C2", "@2", window_name="proj")
    client = FakeSlackClient()
    client.returns["chat_postMessage"] = {"ok": True, "ts": "200.1"}

    await _handle_chat(client, "C2", "U1", [])

    assert thread_router.is_chat_thread("C2", "200.1") is True


@pytest.mark.asyncio
async def test_chat_rejects_unbound_channel():
    client = FakeSlackClient()

    await _handle_chat(client, "C0UNBOUND", "U1", [])

    assert client.call_count("chat_postMessage") == 0
    eph = client.last_call("chat_postEphemeral")
    assert eph is not None
    assert "bound session channel" in eph.kwargs["text"]


def test_chat_thread_reply_is_recognized_for_skip():
    # The text handler's guard: a reply whose thread_ts is a marked chat thread
    # must be recognized so it can be skipped (not forwarded to tmux).
    thread_router.bind_channel("C3", "@3")
    thread_router.mark_chat_thread("C3", "300.1")
    assert thread_router.is_chat_thread("C3", "300.1") is True
    # A reply in an unmarked thread (e.g. a tool-call thread) is NOT skipped.
    assert thread_router.is_chat_thread("C3", "400.2") is False
