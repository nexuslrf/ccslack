import asyncio
import time

import pytest
from slack_bolt.async_app import AsyncApp
from slack_bolt.authorization import AuthorizeResult

from ccslack import window_query
from ccslack.config import config
from ccslack.event_source import dispatch_payload
from ccslack.handlers import agent_input, run_echo, text
from ccslack.handlers.meta import _handle_manual, _handle_run
from ccslack.session import session_manager
from ccslack.slack_client import FakeSlackClient
from ccslack.thread_router import thread_router
from ccslack.window_state_store import WindowState, window_store


@pytest.fixture(autouse=True)
def _clean():
    window_store.window_states.clear()
    thread_router.reset()
    run_echo.reset()
    yield
    window_store.window_states.clear()
    thread_router.reset()
    run_echo.reset()


# ── store ────────────────────────────────────────────────────────────────


def test_input_mode_defaults_to_auto():
    assert window_store.get_input_mode("@1") == "auto"


def test_set_and_toggle_input_mode():
    window_store.set_input_mode("@1", "manual")
    assert window_store.get_input_mode("@1") == "manual"
    assert window_store.toggle_input_mode("@1") == "auto"
    assert window_store.toggle_input_mode("@1") == "manual"


def test_set_input_mode_rejects_bad_value():
    with pytest.raises(ValueError):
        window_store.set_input_mode("@1", "bogus")


def test_input_mode_persists_only_when_non_default():
    state = WindowState(session_id="s", cwd="/x")
    assert "input_mode" not in state.to_dict()
    state.input_mode = "manual"
    assert state.to_dict()["input_mode"] == "manual"
    assert WindowState.from_dict(state.to_dict()).input_mode == "manual"


# ── /ccslack manual ────────────────────────────────────────────────────────


def _bind(channel: str, window: str) -> None:
    session_manager.set_window_provider(window, "claude", cwd="/proj")
    thread_router.bind_channel(channel, window, window_name="proj")


@pytest.mark.asyncio
async def test_manual_command_sets_and_toggles():
    _bind("C1", "@1")
    client = FakeSlackClient()

    await _handle_manual(client, "C1", "U1", ["on"])
    assert window_store.get_input_mode("@1") == "manual"

    await _handle_manual(client, "C1", "U1", [])  # no-arg toggles back
    assert window_store.get_input_mode("@1") == "auto"


@pytest.mark.asyncio
async def test_manual_command_rejects_unbound():
    client = FakeSlackClient()
    await _handle_manual(client, "C0NOPE", "U1", ["on"])
    assert "bound session channel" in client.last_call("chat_postEphemeral").kwargs["text"]


@pytest.mark.asyncio
async def test_manual_command_rejects_unknown_arg():
    _bind("C1", "@1")
    client = FakeSlackClient()
    await _handle_manual(client, "C1", "U1", ["sometimes"])
    assert "unknown mode" in client.last_call("chat_postEphemeral").kwargs["text"]


# ── /ccslack run ───────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_run_forwards_prompt(monkeypatch):
    _bind("C1", "@1")
    sent = {}

    async def _fake_deliver(_client, channel, window, text_, *, slack_ts=None):
        sent.update(channel=channel, window=window, text=text_, slack_ts=slack_ts)
        return True

    async def _live(_wid):
        return object()

    monkeypatch.setattr(agent_input, "deliver_to_agent", _fake_deliver)
    monkeypatch.setattr("ccslack.handlers.meta.tmux_manager.find_window_by_id", _live)
    client = FakeSlackClient()

    await _handle_run(client, "C1", "U1", "run please build the project")

    assert sent["window"] == "@1"
    assert sent["text"] == "please build the project"
    assert sent["slack_ts"] is None
    assert "sent to the agent" in client.last_call("chat_postEphemeral").kwargs["text"]
    # run is quiet — it registers one echo suppression for the window.
    assert run_echo.consume_user_echo_suppression("@1") is True
    assert run_echo.consume_user_echo_suppression("@1") is False


@pytest.mark.asyncio
async def test_run_failure_does_not_suppress_echo(monkeypatch):
    _bind("C1", "@1")

    async def _fail(*a, **k):
        return False

    async def _live(_wid):
        return object()

    monkeypatch.setattr(agent_input, "deliver_to_agent", _fail)
    monkeypatch.setattr("ccslack.handlers.meta.tmux_manager.find_window_by_id", _live)
    client = FakeSlackClient()

    await _handle_run(client, "C1", "U1", "run do it")

    assert "couldn't reach" in client.last_call("chat_postEphemeral").kwargs["text"]
    assert run_echo.consume_user_echo_suppression("@1") is False  # no suppression


def test_run_echo_consume_is_fifo_and_bounded():
    run_echo.suppress_next_user_echo("@1")
    run_echo.suppress_next_user_echo("@1")
    assert run_echo.consume_user_echo_suppression("@1") is True
    assert run_echo.consume_user_echo_suppression("@1") is True
    assert run_echo.consume_user_echo_suppression("@1") is False


def test_run_echo_expires(monkeypatch):
    import ccslack.handlers.run_echo as re_mod

    run_echo.suppress_next_user_echo("@1")  # deadline = now + TTL
    # Jump past the TTL — the stale token must not swallow a later real echo.
    future = time.monotonic() + 100
    monkeypatch.setattr(re_mod.time, "monotonic", lambda: future)
    assert run_echo.consume_user_echo_suppression("@1") is False


@pytest.mark.asyncio
async def test_run_requires_prompt(monkeypatch):
    _bind("C1", "@1")
    client = FakeSlackClient()
    await _handle_run(client, "C1", "U1", "run   ")
    assert "usage" in client.last_call("chat_postEphemeral").kwargs["text"]


@pytest.mark.asyncio
async def test_run_rejects_dead_window(monkeypatch):
    _bind("C1", "@1")

    async def _dead(_wid):
        return None

    monkeypatch.setattr("ccslack.handlers.meta.tmux_manager.find_window_by_id", _dead)
    client = FakeSlackClient()
    await _handle_run(client, "C1", "U1", "run do it")
    assert "restore" in client.last_call("chat_postEphemeral").kwargs["text"]


# ── deliver_to_agent ───────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_deliver_to_agent_non_shell(monkeypatch):
    keys = {}

    async def _send(window, text_, *a, **k):
        keys.update(window=window, text=text_)

    monkeypatch.setattr(agent_input.shell_capture, "is_shell_window", lambda _w: False)
    monkeypatch.setattr(agent_input.tmux_manager, "send_keys", _send)

    ok = await agent_input.deliver_to_agent(object(), "C1", "@1", "hello")
    assert ok is True
    assert keys == {"window": "@1", "text": "hello"}


@pytest.mark.asyncio
async def test_deliver_to_agent_send_failure_returns_false(monkeypatch):
    async def _boom(*a, **k):
        raise OSError("no tmux")

    monkeypatch.setattr(agent_input.shell_capture, "is_shell_window", lambda _w: False)
    monkeypatch.setattr(agent_input.tmux_manager, "send_keys", _boom)

    assert await agent_input.deliver_to_agent(object(), "C1", "@1", "x") is False


# ── message-gate integration (manual vs auto, mention detection) ───────────


async def _authorize(*_a, **_k) -> AuthorizeResult:
    return AuthorizeResult(
        enterprise_id=None,
        team_id="T1",
        bot_token="xoxb-fake",
        bot_id="B1",
        bot_user_id="U0BOT",
    )


def _msg_payload(text_: str) -> dict:
    return {
        "token": "x",
        "team_id": "T1",
        "api_app_id": "A1",
        "type": "event_callback",
        "event": {
            "type": "message",
            "channel": "C1",
            "channel_type": "channel",
            "user": "U1",
            "text": text_,
            "ts": "1700.0001",
        },
        "event_id": "Ev1",
        "event_time": 1,
        "authorizations": [
            {"team_id": "T1", "user_id": "U0BOT", "is_bot": True}
        ],
    }


async def _settle(check, timeout: float = 1.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if check():
            return
        await asyncio.sleep(0.01)


@pytest.fixture
def _wired(monkeypatch):
    monkeypatch.setattr(config, "allowed_users", {"U1"})
    _bind("C1", "@1")
    delivered: list[str] = []

    async def _fake_deliver(_client, _channel, _window, text_, *, slack_ts=None):
        delivered.append(text_)
        return True

    async def _noop_react(*_a, **_k):
        return None

    monkeypatch.setattr(text, "deliver_to_agent", _fake_deliver)
    monkeypatch.setattr(text, "_react", _noop_react)

    app = AsyncApp(
        authorize=_authorize,
        request_verification_enabled=False,
        raise_error_for_unhandled_request=False,
    )
    text.register(app)
    return app, delivered


@pytest.mark.asyncio
async def test_auto_mode_forwards_plain_message(_wired):
    app, delivered = _wired
    await dispatch_payload(app, _msg_payload("hello world"))
    await _settle(lambda: delivered == ["hello world"])
    assert delivered == ["hello world"]


@pytest.mark.asyncio
async def test_manual_mode_blocks_plain_message(_wired):
    app, delivered = _wired
    window_store.set_input_mode("@1", "manual")
    await dispatch_payload(app, _msg_payload("just chatting with humans"))
    await _settle(lambda: bool(delivered))
    assert delivered == []  # not forwarded


@pytest.mark.asyncio
async def test_manual_mode_forwards_mention_stripped(_wired):
    app, delivered = _wired
    window_store.set_input_mode("@1", "manual")
    await dispatch_payload(app, _msg_payload("<@U0BOT> do the thing"))
    await _settle(lambda: bool(delivered))
    assert delivered == ["do the thing"]


@pytest.mark.asyncio
async def test_auto_mode_strips_mention_too(_wired):
    app, delivered = _wired
    await dispatch_payload(app, _msg_payload("<@U0BOT> hi there"))
    await _settle(lambda: bool(delivered))
    assert delivered == ["hi there"]


def test_input_mode_read_via_window_query():
    window_store.set_input_mode("@9", "manual")
    assert window_query.get_input_mode("@9") == "manual"
