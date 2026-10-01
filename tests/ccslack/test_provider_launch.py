import pytest

from ccslack.providers import resolve_launch_command


def test_resolve_returns_base_command():
    assert resolve_launch_command("claude") == "claude"
    assert resolve_launch_command("codex") == "codex"


def test_resolve_never_adds_skip_approvals_flags():
    # ccslack must never auto-enable permissive mode — that is a deliberate,
    # manual choice via `/ccslack relaunch`.
    for provider, flag in (
        ("claude", "--dangerously-skip-permissions"),
        ("codex", "--dangerously-bypass-approvals-and-sandbox"),
        ("gemini", "--yolo"),
        ("cursor", "--force"),
    ):
        assert flag not in resolve_launch_command(provider)


def test_resolve_honors_command_override(monkeypatch):
    monkeypatch.setenv("CCSLACK_CLAUDE_COMMAND", "my-claude-wrapper")
    assert resolve_launch_command("claude") == "my-claude-wrapper"


def test_resolve_unknown_provider_falls_back_to_claude():
    assert resolve_launch_command("nope") == "claude"


def test_enter_delay_scales_with_length():
    """The pre-Enter gap must grow with message length — a fixed 500ms let
    long pastes swallow the Enter (message sat unsubmitted in the composer)."""
    import pytest

    from ccslack.tmux_manager import (
        _ENTER_DELAY_CHARS,
        _MAX_ENTER_EXTRA_DELAY,
        _send_enter_delay,
    )

    # Short prompt: base 0.5s.
    assert _send_enter_delay("hi") == pytest.approx(0.5025)
    # 800 chars: base + 1s.
    assert _send_enter_delay("x" * 800) == pytest.approx(1.5)
    # 4000 chars: capped at base + 2.5s.
    assert _send_enter_delay("x" * 4000) == pytest.approx(3.0)
    assert _MAX_ENTER_EXTRA_DELAY == 2.5
    assert _ENTER_DELAY_CHARS == 800.0


def test_enter_attempts_threshold():
    from ccslack.tmux_manager import _enter_attempts

    assert _enter_attempts("short message") == 1
    assert _enter_attempts("x" * 999) == 1
    assert _enter_attempts("x" * 1001) == 3
    assert _enter_attempts("x" * 5000) == 3  # capped


@pytest.mark.asyncio
async def test_long_message_sends_multiple_enters(monkeypatch):
    """A long paste must get repeated submit Enters — the first is swallowed
    by some composers; retries rescue it (no-op when already submitted)."""
    import asyncio

    from ccslack.tmux_manager import tmux_manager

    enters: list[dict] = []

    def _fake_pane_send(_window_id, chars, *, enter, literal):
        enters.append({"chars": chars, "enter": enter, "literal": literal})
        return True

    async def _noop_vim(_wid):
        return None

    async def _short_sleep(_s):
        pass

    monkeypatch.setattr(tmux_manager, "_pane_send", _fake_pane_send)
    monkeypatch.setattr(tmux_manager, "_ensure_vim_insert_mode", _noop_vim)
    monkeypatch.setattr(asyncio, "sleep", _short_sleep)

    ok = await tmux_manager._send_literal_then_enter_locked("@1", "x" * 2000)
    assert ok is True
    enter_presses = [e for e in enters if e["enter"] and not e["literal"]]
    assert len(enter_presses) == 3  # multi-Enter retry
    # Short message: single Enter.
    enters.clear()
    ok = await tmux_manager._send_literal_then_enter_locked("@1", "hi")
    assert ok is True
    enter_presses = [e for e in enters if e["enter"] and not e["literal"]]
    assert len(enter_presses) == 1
