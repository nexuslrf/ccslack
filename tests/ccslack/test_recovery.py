import pytest

from ccslack.handlers.recovery import (
    _build_launch_args,
    _build_launch_args_for,
    _slug_from_cwd,
    parse_channel_topic,
    recover_channel_context,
)
from ccslack.session import session_manager
from ccslack.slack_client import FakeSlackClient
from ccslack.window_state_store import window_store

CLAUDE_ID = "01234567-89ab-cdef-0123-456789abcdef"
CODEX_ID = "019e50a6-a8e1-7521-950b-a71046f31bc7"


@pytest.fixture
def seeded():
    window_store.window_states.clear()

    def seed(window_id: str, provider: str, session_id: str) -> None:
        session_manager.set_window_provider(window_id, provider, cwd="/tmp/x")
        window_store.get_window_state(window_id).session_id = session_id

    yield seed
    window_store.window_states.clear()


def test_fresh_returns_empty(seeded):
    seeded("@1", "claude", CLAUDE_ID)
    assert _build_launch_args("@1", "fresh") == ""


def test_claude_resume_uses_flag(seeded):
    seeded("@1", "claude", CLAUDE_ID)
    assert _build_launch_args("@1", "resume") == f"--resume {CLAUDE_ID}"


def test_claude_continue_uses_flag(seeded):
    seeded("@1", "claude", CLAUDE_ID)
    assert _build_launch_args("@1", "continue") == "--continue"


def test_codex_resume_uses_subcommand(seeded):
    seeded("@2", "codex", CODEX_ID)
    assert _build_launch_args("@2", "resume") == f"resume {CODEX_ID}"


def test_codex_continue_uses_resume_last(seeded):
    seeded("@2", "codex", CODEX_ID)
    assert _build_launch_args("@2", "continue") == "resume --last"


def test_resume_without_session_id_falls_back_to_continue(seeded):
    seeded("@3", "codex", "")
    assert _build_launch_args("@3", "resume") == "resume --last"


def test_unknown_window_returns_empty():
    window_store.window_states.clear()
    assert _build_launch_args("@999", "resume") == ""


# --- topic parsing / re-adoption -------------------------------------------


def test_parse_channel_topic_basic():
    assert parse_channel_topic("claude · /home/me/proj") == (
        "claude",
        "/home/me/proj",
    )


def test_parse_channel_topic_lowercases_provider():
    assert parse_channel_topic("Codex · /tmp/x") == ("codex", "/tmp/x")


def test_parse_channel_topic_rejects_garbage():
    assert parse_channel_topic("") is None
    assert parse_channel_topic("just some words") is None
    assert parse_channel_topic("claude ·  ") is None
    assert parse_channel_topic(" · /tmp/x") is None


def test_build_launch_args_for_shell_is_empty():
    assert _build_launch_args_for("shell", "", "continue") == ""


def test_build_launch_args_for_codex_continue():
    assert _build_launch_args_for("codex", "", "continue") == "resume --last"


def test_build_launch_args_for_claude_resume_with_id():
    assert _build_launch_args_for("claude", CLAUDE_ID, "resume") == (
        f"--resume {CLAUDE_ID}"
    )


def test_slug_from_cwd():
    assert _slug_from_cwd("/home/me/My Project") == "my-project"
    assert _slug_from_cwd("/tmp/") == "tmp"
    assert _slug_from_cwd("") == "session"


@pytest.mark.asyncio
async def test_recover_channel_context_reads_topic():
    client = FakeSlackClient()
    client.returns["conversations_info"] = {
        "ok": True,
        "channel": {"topic": {"value": "codex · /nvmepool/ruofan/projects_leo"}},
    }
    ctx = await recover_channel_context(client, "C0LOST")
    assert ctx == ("codex", "/nvmepool/ruofan/projects_leo")


@pytest.mark.asyncio
async def test_recover_channel_context_empty_topic_returns_none():
    client = FakeSlackClient()
    client.returns["conversations_info"] = {
        "ok": True,
        "channel": {"topic": {"value": ""}},
    }
    assert await recover_channel_context(client, "C0LOST") is None
