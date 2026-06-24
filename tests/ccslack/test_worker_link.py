import asyncio
import time

import pytest
from slack_bolt.async_app import AsyncApp
from slack_bolt.authorization import AuthorizeResult

from ccslack import link
from ccslack.event_source import RouterLinkSource
from ccslack.thread_router import thread_router

_MESSAGE = {
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


async def _authorize(*_a, **_k) -> AuthorizeResult:
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


async def _settle(check, timeout: float = 1.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if check():
            return
        await asyncio.sleep(0.01)


@pytest.fixture(autouse=True)
def _clean():
    thread_router.reset()
    yield
    thread_router.reset()


@pytest.mark.asyncio
async def test_worker_link_hello_event_ping_and_bind_push():
    thread_router.bind_channel("C1", "@1")  # owned before the router connects
    app = _app()
    seen: list[dict] = []

    @app.event("message")
    async def _on(event) -> None:  # noqa: ANN001
        seen.append(event)

    source = RouterLinkSource(app, host="gpu1", port=0)
    await source.start()
    try:
        reader, writer = await asyncio.open_connection("127.0.0.1", source.port)

        # hello snapshot on connect
        hello = await link.read_msg(reader)
        assert hello["t"] == "hello"
        assert hello["host"] == "gpu1"
        assert "C1" in hello["channels"]

        # forwarded event → dispatched into the app
        await link.write_msg(writer, link.event(_MESSAGE))
        await _settle(lambda: len(seen) == 1)
        assert seen[0]["text"] == "hi from router"

        # ping → pong
        await link.write_msg(writer, {"t": "ping"})
        assert (await link.read_msg(reader))["t"] == "pong"

        # a new binding is pushed live
        thread_router.bind_channel("C2", "@2")
        pushed = await link.read_msg(reader)
        assert pushed == {"t": "bind", "channel": "C2"}

        # and an unbind too
        thread_router.unbind_channel("C2")
        assert await link.read_msg(reader) == {"t": "unbind", "channel": "C2"}

        writer.close()
    finally:
        await source.stop()


@pytest.mark.asyncio
async def test_stop_unregisters_binding_listener():
    app = _app()
    source = RouterLinkSource(app, host="h", port=0)
    await source.start()
    assert thread_router._binding_listeners  # registered
    await source.stop()
    assert not thread_router._binding_listeners  # cleaned up
