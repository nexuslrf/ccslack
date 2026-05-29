import pytest
from slack_sdk.errors import SlackApiError

from ccslack.slack_client import FakeSlackClient
from ccslack.slack_sender import safe_close_message


def _err(code: str) -> SlackApiError:
    """Build a SlackApiError carrying ``response["error"] = code``."""

    class _Resp:
        data = {"ok": False, "error": code}

        def get(self, key: str, default=None):
            return self.data.get(key, default)

        def __getitem__(self, key: str):
            return self.data[key]

    return SlackApiError(message=code, response=_Resp())


@pytest.mark.asyncio
async def test_delete_succeeds_no_fallback():
    client = FakeSlackClient()
    client.returns["chat_delete"] = {"ok": True}
    await safe_close_message(client, channel="C0", ts="1.1", label="picker")
    assert client.call_count("chat_delete") == 1
    assert client.call_count("chat_update") == 0


@pytest.mark.asyncio
async def test_delete_failure_falls_back_to_update():
    client = FakeSlackClient()
    client.set_side_effect("chat_delete", [_err("cant_delete_message")])
    client.returns["chat_update"] = {"ok": True}
    await safe_close_message(client, channel="C0", ts="1.1", label="toolbar")
    assert client.call_count("chat_delete") == 1
    assert client.call_count("chat_update") == 1
    update = client.last_call("chat_update")
    assert update is not None
    assert "toolbar closed" in update.kwargs["text"]


@pytest.mark.asyncio
async def test_both_failures_logged_no_raise():
    """Both delete and update fail — helper swallows so the action handler
    that called it doesn't blow up."""
    client = FakeSlackClient()
    client.set_side_effect("chat_delete", [_err("compliance_exports_prevent_deletion")])
    client.set_side_effect("chat_update", [_err("message_not_found")])
    # Doesn't raise.
    await safe_close_message(client, channel="C0", ts="1.1", label="picker")
    assert client.call_count("chat_delete") == 1
    assert client.call_count("chat_update") == 1
