import pytest

from ccslack.config import config
from ccslack.handlers.meta import _handle_kill, _meta_surface_hint
from ccslack.slack_client import FakeSlackClient


def test_hint_channel_mode(monkeypatch):
    monkeypatch.setattr(config, "meta_channel_id", "C0METATEST")
    monkeypatch.setattr(config, "meta_surface", "channel")
    assert _meta_surface_hint() == "the meta channel (<#C0METATEST>)"


def test_hint_hybrid_mode(monkeypatch):
    monkeypatch.setattr(config, "meta_channel_id", "C0METATEST")
    monkeypatch.setattr(config, "meta_surface", "hybrid")
    assert _meta_surface_hint() == "the meta channel (<#C0METATEST>) or the app's DM"


def test_hint_dm_mode(monkeypatch):
    monkeypatch.setattr(config, "meta_surface", "dm")
    assert _meta_surface_hint() == "the app's DM"


@pytest.mark.asyncio
async def test_kill_all_rejected_from_dm_in_channel_mode(monkeypatch):
    monkeypatch.setattr(config, "meta_channel_id", "C0METATEST")
    monkeypatch.setattr(config, "meta_surface", "channel")
    client = FakeSlackClient()

    await _handle_kill(client, "D0DM", "U0ALLOWED", ["--all"])

    text = client.last_call("chat_postEphemeral").kwargs["text"]
    assert "only works in" in text
    assert "the meta channel" in text


@pytest.mark.asyncio
async def test_kill_all_accepted_from_dm_in_hybrid_mode(monkeypatch):
    monkeypatch.setattr(config, "meta_channel_id", "C0METATEST")
    monkeypatch.setattr(config, "meta_surface", "hybrid")
    client = FakeSlackClient()

    # Past the surface gate, the no-`--confirm` path asks to re-run with
    # --confirm (proving the DM was accepted as a management surface).
    await _handle_kill(client, "D0DM", "U0ALLOWED", ["--all"])

    text = client.last_call("chat_postEphemeral").kwargs["text"]
    assert "kill --all --confirm" in text


def test_repo_dir_detection():
    from ccslack.handlers.meta import _ccslack_repo_dir

    repo = _ccslack_repo_dir()
    assert repo is not None
    assert (repo / "pyproject.toml").is_file()
    assert (repo / "src" / "ccslack").is_dir()


def test_workers_parsed_from_config(monkeypatch):
    """The fleet branch parses CCSLACK_WORKERS via the shared router_link parser."""
    from ccslack.router_link import parse_workers

    specs = parse_workers("gpu1=gpu1.nvidia.com,gpu2=ruofanl@gpu2", 8765)
    assert [(s.host, s.ssh_target) for s in specs] == [
        ("gpu1", "gpu1.nvidia.com"),
        ("gpu2", "ruofanl@gpu2"),
    ]
