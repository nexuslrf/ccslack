import asyncio
import sys

import pytest

from ccslack import ssh_auth
from ccslack.config import config
from ccslack.handlers.ssh_prompt import (
    _handle_option,
    _handle_passcode_submit,
    build_prompt_blocks,
)
from ccslack.slack_client import FakeSlackClient

_DUO = (
    "(ruofan@trillium-gpu.alliancecan.ca) Duo two-factor login for ruofan\n"
    "\n"
    "Enter a passcode or select one of the following options:\n"
    "\n"
    " 1. Duo Push to Xperia (Android)\n"
    "\n"
    "Passcode or option (1-1): "
)


def test_strip_ansi():
    assert ssh_auth.strip_ansi("a\x1b[31mb\x1b[0m\rc") == "abc"


def test_looks_like_prompt_matches_duo(monkeypatch):
    from ccslack.config import config

    assert ssh_auth.looks_like_prompt(_DUO, config.ssh_prompt_re) is True


def test_looks_like_prompt_password():
    assert ssh_auth.looks_like_prompt(
        "ruofan@host's password: ", r"(?i)password.*:\s*$"
    )


def test_looks_like_prompt_false_for_plain_output():
    assert ssh_auth.looks_like_prompt("just some log line\n", r"(?i)password.*:\s*$") is False


def test_parse_options_duo():
    assert ssh_auth.parse_options(_DUO) == [("1", "Duo Push to Xperia (Android)")]


def test_parse_options_multiple():
    text = " 1. Push to phone\n 2. Phone call\n 3. SMS passcodes\n"
    assert ssh_auth.parse_options(text) == [
        ("1", "Push to phone"),
        ("2", "Phone call"),
        ("3", "SMS passcodes"),
    ]


@pytest.mark.asyncio
async def test_responder_registry_roundtrip():
    received: list[str] = []

    async def _r(text: str) -> bool:
        received.append(text)
        return True

    ssh_auth.register_responder("gpu1", _r)
    assert await ssh_auth.respond("gpu1", "1") is True
    assert received == ["1"]
    assert await ssh_auth.respond("ghost", "x") is False  # no responder
    ssh_auth.unregister_responder("gpu1")
    assert await ssh_auth.respond("gpu1", "1") is False


def test_build_prompt_blocks_has_option_and_passcode():
    blocks = build_prompt_blocks("gpu1", _DUO, [("1", "Duo Push to Xperia (Android)")])
    action_ids = [
        e["action_id"]
        for b in blocks
        if b["type"] == "actions"
        for e in b["elements"]
    ]
    assert "ccslack_ssh_opt:1" in action_ids
    assert "ccslack_ssh_pass" in action_ids
    # The option button carries host|number for delivery.
    opt = next(
        e
        for b in blocks
        if b["type"] == "actions"
        for e in b["elements"]
        if e["action_id"] == "ccslack_ssh_opt:1"
    )
    assert opt["value"] == "gpu1|1"


@pytest.mark.asyncio
async def test_pty_process_captures_prompt_and_writes_response():
    # A tiny interactive program: print a prompt (no newline), read a line, echo.
    prog = (
        "import sys; "
        "sys.stdout.write('Passcode or option (1-1): '); sys.stdout.flush(); "
        "ans = sys.stdin.readline().strip(); "
        "sys.stdout.write('GOT:' + ans + '\\n'); sys.stdout.flush()"
    )
    seen: list[str] = []

    async def _on_output(chunk: str) -> None:
        seen.append(chunk)

    proc = ssh_auth.PtyProcess([sys.executable, "-c", prog], _on_output)
    await proc.start()
    # Wait for the prompt.
    for _ in range(200):
        if any("Passcode" in c for c in seen):
            break
        await asyncio.sleep(0.01)
    assert any("Passcode" in c for c in seen)

    assert await proc.write("1") is True
    for _ in range(200):
        if any("GOT:1" in c for c in seen):
            break
        await asyncio.sleep(0.01)
    await proc.wait()
    await proc.stop()
    assert any("GOT:1" in c for c in seen)


@pytest.mark.asyncio
async def test_handle_option_delivers_to_tunnel(monkeypatch):
    monkeypatch.setattr(config, "allowed_users", {"U1"})
    written: list[str] = []

    async def _responder(text: str) -> bool:
        written.append(text)
        return True

    ssh_auth.register_responder("gpu1", _responder)
    client = FakeSlackClient()
    body = {
        "user": {"id": "U1"},
        "channel": {"id": "C0META"},
        "message": {"ts": "111.2"},
        "actions": [{"action_id": "ccslack_ssh_opt:1", "value": "gpu1|1"}],
    }

    await _handle_option(body, client)

    assert written == ["1"]
    upd = client.last_call("chat_update")
    assert upd is not None and "gpu1" in upd.kwargs["text"]


@pytest.mark.asyncio
async def test_handle_option_rejects_non_allowed(monkeypatch):
    monkeypatch.setattr(config, "allowed_users", {"U_ADMIN"})
    written: list[str] = []

    async def _responder(text: str) -> bool:
        written.append(text)
        return True

    ssh_auth.register_responder("gpu1", _responder)
    body = {
        "user": {"id": "U_RANDOM"},
        "channel": {"id": "C0META"},
        "actions": [{"action_id": "ccslack_ssh_opt:1", "value": "gpu1|1"}],
    }

    await _handle_option(body, FakeSlackClient())
    assert written == []  # unauthorized: nothing delivered


@pytest.mark.asyncio
async def test_handle_passcode_submit_delivers(monkeypatch):
    monkeypatch.setattr(config, "allowed_users", {"U1"})
    written: list[str] = []

    async def _responder(text: str) -> bool:
        written.append(text)
        return True

    ssh_auth.register_responder("gpu1", _responder)
    body = {"user": {"id": "U1"}}
    view = {
        "private_metadata": "gpu1|C0META|111.2",
        "state": {"values": {"passcode_block": {"passcode": {"value": "123456"}}}},
    }

    await _handle_passcode_submit(body, view, FakeSlackClient())
    assert written == ["123456"]
