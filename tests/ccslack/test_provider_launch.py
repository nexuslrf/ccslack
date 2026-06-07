import pytest

from ccslack.providers import has_yolo_mode, resolve_launch_command


def test_has_yolo_mode_supported_providers():
    assert has_yolo_mode("claude")
    assert has_yolo_mode("codex")
    assert has_yolo_mode("gemini")


def test_has_yolo_mode_unsupported_providers():
    assert not has_yolo_mode("shell")
    assert not has_yolo_mode("pi")
    assert not has_yolo_mode("unknown")


def test_normal_mode_has_no_yolo_flag():
    command = resolve_launch_command("claude", approval_mode="normal")
    assert "--dangerously-skip-permissions" not in command


@pytest.mark.parametrize(
    ("provider", "flag"),
    [
        ("claude", "--dangerously-skip-permissions"),
        ("codex", "--dangerously-bypass-approvals-and-sandbox"),
        ("gemini", "--yolo"),
    ],
)
def test_yolo_mode_appends_provider_flag(provider, flag):
    command = resolve_launch_command(provider, approval_mode="yolo")
    assert flag in command


def test_yolo_flag_not_duplicated_on_override(monkeypatch):
    monkeypatch.setenv(
        "CCSLACK_CLAUDE_COMMAND", "claude --dangerously-skip-permissions"
    )
    command = resolve_launch_command("claude", approval_mode="yolo")
    assert command.count("--dangerously-skip-permissions") == 1


def test_yolo_appends_to_custom_override(monkeypatch):
    monkeypatch.setenv("CCSLACK_CODEX_COMMAND", "codex")
    command = resolve_launch_command("codex", approval_mode="yolo")
    assert command == "codex --dangerously-bypass-approvals-and-sandbox"
