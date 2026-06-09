import pytest

from ccslack.handlers.recovery import (
    _build_launch_args,
    _build_launch_args_for,
    _same_cwd,
    _slug_from_cwd,
    parse_channel_topic,
    recover_channel_context,
    restore_window,
)
from ccslack.session import session_manager
from ccslack.slack_client import FakeSlackClient
from ccslack.thread_router import thread_router
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


# --- restore_window topic fallback (regression for startup-prune bug) ---------


@pytest.fixture
def _clean_state():
    window_store.window_states.clear()
    thread_router.reset()
    yield
    window_store.window_states.clear()
    thread_router.reset()


@pytest.mark.asyncio
async def test_restore_window_falls_back_to_topic_when_cwd_missing(
    monkeypatch, _clean_state
):
    thread_router.bind_channel("C0WIPED", "@55", window_name="proj")

    client = FakeSlackClient()
    client.returns["conversations_info"] = {
        "ok": True,
        "channel": {"topic": {"value": "codex · /recovered/path"}},
    }

    spawned = []

    async def _fake_restore_in_channel(c, ch, *, provider, cwd, **kw):
        spawned.append((provider, cwd))
        return "@99"

    monkeypatch.setattr(
        "ccslack.handlers.recovery.restore_in_channel", _fake_restore_in_channel
    )

    result = await restore_window(client, "C0WIPED", "@55", mode="continue")

    assert result == "@99"
    assert spawned == [("codex", "/recovered/path")]


@pytest.mark.asyncio
async def test_restore_window_returns_none_when_topic_also_missing(
    monkeypatch, _clean_state
):
    thread_router.bind_channel("C0NOTOPIC", "@56", window_name="proj")

    client = FakeSlackClient()
    client.returns["conversations_info"] = {
        "ok": True,
        "channel": {"topic": {"value": ""}},
    }

    result = await restore_window(client, "C0NOTOPIC", "@56", mode="continue")

    assert result is None


def test_same_cwd_normalizes_and_rejects_empty():
    assert _same_cwd("/a/b", "/a/b/") is True
    assert _same_cwd("/a/b/../b", "/a/b") is True
    assert _same_cwd("/a/b", "/a/c") is False
    assert _same_cwd("", "/a/b") is False
    assert _same_cwd("/a/b", "") is False


@pytest.mark.asyncio
async def test_restore_window_topic_overrides_drifted_state_cwd(
    monkeypatch, _clean_state
):
    thread_router.bind_channel("C0DRIFT", "@60", window_name="proj")
    session_manager.set_window_provider("@60", "claude", cwd="/wrong/path")
    window_store.get_window_state("@60").session_id = CLAUDE_ID

    client = FakeSlackClient()
    client.returns["conversations_info"] = {
        "ok": True,
        "channel": {"topic": {"value": "codex · /right/path"}},
    }

    spawned = []

    async def _fake_restore_in_channel(c, ch, *, provider, cwd, session_id, **kw):
        spawned.append((provider, cwd, session_id))
        return "@260"

    monkeypatch.setattr(
        "ccslack.handlers.recovery.restore_in_channel", _fake_restore_in_channel
    )

    result = await restore_window(client, "C0DRIFT", "@60", mode="resume")

    assert result == "@260"
    assert spawned == [("codex", "/right/path", "")]


@pytest.mark.asyncio
async def test_restore_window_keeps_session_id_when_topic_cwd_matches(
    monkeypatch, _clean_state
):
    thread_router.bind_channel("C0KEEP", "@61", window_name="proj")
    session_manager.set_window_provider("@61", "claude", cwd="/same/path")
    window_store.get_window_state("@61").session_id = CLAUDE_ID

    client = FakeSlackClient()
    client.returns["conversations_info"] = {
        "ok": True,
        "channel": {"topic": {"value": "claude · /same/path"}},
    }

    spawned = []

    async def _fake_restore_in_channel(c, ch, *, provider, cwd, session_id, **kw):
        spawned.append((provider, cwd, session_id))
        return "@261"

    monkeypatch.setattr(
        "ccslack.handlers.recovery.restore_in_channel", _fake_restore_in_channel
    )

    result = await restore_window(client, "C0KEEP", "@61", mode="resume")

    assert result == "@261"
    assert spawned == [("claude", "/same/path", CLAUDE_ID)]
