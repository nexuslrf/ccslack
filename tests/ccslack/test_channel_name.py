from pathlib import Path

from ccslack.config import config
from ccslack.handlers.meta import _channel_name_for


def test_default_prefix(monkeypatch):
    monkeypatch.setattr(config, "channel_prefix", "ccslack")
    assert _channel_name_for(Path("/home/me/VRender")) == "ccslack-vrender"


def test_custom_prefix(monkeypatch):
    monkeypatch.setattr(config, "channel_prefix", "dev")
    assert _channel_name_for(Path("/home/me/My Project")) == "dev-my-project"


def test_empty_prefix_uses_bare_slug(monkeypatch):
    monkeypatch.setattr(config, "channel_prefix", "")
    assert _channel_name_for(Path("/home/me/VRender")) == "vrender"


def test_prefix_is_sanitized(monkeypatch):
    monkeypatch.setattr(config, "channel_prefix", "Team Alpha")
    assert _channel_name_for(Path("/x/api")) == "team-alpha-api"
