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
