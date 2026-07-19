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
