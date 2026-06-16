import pytest

from ccslack.handlers.messaging_pipeline import turn_threads
from ccslack.slack_client import FakeSlackClient


@pytest.fixture(autouse=True)
def _clear():
    turn_threads.reset_for_testing()
    yield
    turn_threads.reset_for_testing()


@pytest.fixture
def client():
    c = FakeSlackClient()
    c.returns["chat_postMessage"] = {"ok": True, "ts": "1700.0001"}
    c.returns["chat_update"] = {"ok": True}
    return c


@pytest.mark.asyncio
async def test_first_threadable_creates_parent(client):
    ts = await turn_threads.thread_parent_for(client, "C0", is_tool=True)
    assert ts == "1700.0001"
    assert turn_threads.has_active_turn("C0")
    assert client.call_count("chat_postMessage") == 1


@pytest.mark.asyncio
async def test_subsequent_messages_reuse_parent(client):
    ts1 = await turn_threads.thread_parent_for(client, "C0", is_tool=True)
    ts2 = await turn_threads.thread_parent_for(client, "C0", is_tool=False)
    ts3 = await turn_threads.thread_parent_for(client, "C0", is_tool=True)
    assert ts1 == ts2 == ts3
    # Only one parent posted for the whole turn.
    assert client.call_count("chat_postMessage") == 1


@pytest.mark.asyncio
async def test_parent_carries_close_button(client):
    await turn_threads.thread_parent_for(client, "C0", is_tool=True)
    post = client.last_call("chat_postMessage")
    assert "ccslack_purge_thread" in str(post.kwargs.get("blocks", ""))


@pytest.mark.asyncio
async def test_end_turn_keeps_close_button(client):
    await turn_threads.thread_parent_for(client, "C0", is_tool=True)
    await turn_threads.end_turn(client, "C0")
    upd = client.last_call("chat_update")
    assert "ccslack_purge_thread" in str(upd.kwargs.get("blocks", ""))


@pytest.mark.asyncio
async def test_end_turn_summarises_tool_count(client):
    await turn_threads.thread_parent_for(client, "C0", is_tool=True)
    await turn_threads.thread_parent_for(client, "C0", is_tool=True)
    await turn_threads.thread_parent_for(client, "C0", is_tool=False)  # thinking
    await turn_threads.end_turn(client, "C0")

    assert not turn_threads.has_active_turn("C0")
    upd = client.last_call("chat_update")
    assert upd is not None
    assert "2 tool calls" in upd.kwargs["text"]


@pytest.mark.asyncio
async def test_end_turn_zero_tools_is_activity_summary(client):
    await turn_threads.thread_parent_for(client, "C0", is_tool=False)  # only thinking
    await turn_threads.end_turn(client, "C0")
    upd = client.last_call("chat_update")
    assert "Agent activity" in upd.kwargs["text"]


@pytest.mark.asyncio
async def test_singular_tool_call_label(client):
    await turn_threads.thread_parent_for(client, "C0", is_tool=True)
    await turn_threads.end_turn(client, "C0")
    upd = client.last_call("chat_update")
    assert "1 tool call" in upd.kwargs["text"]
    assert "1 tool calls" not in upd.kwargs["text"]


@pytest.mark.asyncio
async def test_end_turn_no_op_without_turn(client):
    await turn_threads.end_turn(client, "C0NONE")
    assert client.call_count("chat_update") == 0


@pytest.mark.asyncio
async def test_note_user_message_ends_turn(client):
    await turn_threads.thread_parent_for(client, "C0", is_tool=True)
    await turn_threads.note_user_message(client, "C0")
    assert not turn_threads.has_active_turn("C0")
    assert client.call_count("chat_update") == 1


@pytest.mark.asyncio
async def test_clear_channel_drops_state_silently(client):
    await turn_threads.thread_parent_for(client, "C0", is_tool=True)
    turn_threads.clear_channel("C0")
    assert not turn_threads.has_active_turn("C0")
    # clear_channel does not touch Slack.
    assert client.call_count("chat_update") == 0


@pytest.mark.asyncio
async def test_parent_post_failure_returns_none(client):
    client.returns["chat_postMessage"] = {"ok": False}  # no ts
    ts = await turn_threads.thread_parent_for(client, "C0", is_tool=True)
    assert ts is None
    assert not turn_threads.has_active_turn("C0")
