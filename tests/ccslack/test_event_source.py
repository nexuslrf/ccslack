import asyncio
import time

import pytest
from slack_bolt.async_app import AsyncApp
from slack_bolt.authorization import AuthorizeResult

from ccslack.event_source import dispatch_payload


async def _settle(check, timeout: float = 1.0) -> None:
    """Wait for a backgrounded Bolt listener to run (process_before_response)."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if check():
            return
        await asyncio.sleep(0.01)


async def _authorize(*_args, **_kwargs) -> AuthorizeResult:
    # Static authorize so the app never calls auth.test over the network.
    return AuthorizeResult(
        enterprise_id=None,
        team_id="T1",
        bot_token="xoxb-fake",
        bot_id="B1",
        bot_user_id="U0BOT",
    )


def _app() -> AsyncApp:
    return AsyncApp(
        authorize=_authorize,
        request_verification_enabled=False,
        raise_error_for_unhandled_request=False,
    )


@pytest.mark.asyncio
async def test_dispatch_payload_routes_message_event_to_handler():
    app = _app()
    seen: list[dict] = []

    @app.event("message")
    async def _on_message(event) -> None:  # noqa: ANN001
        seen.append(event)

    payload = {
        "token": "x",
        "team_id": "T1",
        "api_app_id": "A1",
        "type": "event_callback",
        "event": {
            "type": "message",
            "channel": "C1",
            "channel_type": "channel",
            "user": "U1",
            "text": "hi from router",
            "ts": "1700.0001",
        },
        "event_id": "Ev1",
        "event_time": 1,
        "authorizations": [
            {"enterprise_id": None, "team_id": "T1", "user_id": "U0BOT", "is_bot": True}
        ],
    }

    await dispatch_payload(app, payload)
    await _settle(lambda: len(seen) == 1)

    assert len(seen) == 1
    assert seen[0]["text"] == "hi from router"


@pytest.mark.asyncio
async def test_dispatch_payload_routes_slash_command_to_handler():
    app = _app()
    seen: list[dict] = []

    @app.command("/ccslack")
    async def _on_cmd(ack, command) -> None:  # noqa: ANN001
        await ack()
        seen.append(command)

    payload = {
        "token": "x",
        "team_id": "T1",
        "command": "/ccslack",
        "text": "list",
        "channel_id": "C1",
        "user_id": "U1",
        "trigger_id": "T.123",
        "response_url": "https://example.invalid/r",
    }

    await dispatch_payload(app, payload)
    await _settle(lambda: len(seen) == 1)

    assert len(seen) == 1
    assert seen[0]["text"] == "list"
