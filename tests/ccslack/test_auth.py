import pytest

from ccslack.handlers.auth import (
    is_authorized,
    is_dm_channel,
    is_meta_authorized,
    is_meta_surface,
)
from ccslack.thread_router import ThreadRouter, install_thread_router


@pytest.fixture
def bound_router():
    """Install a router with one bound session channel for the test."""

    def schedule_save() -> None:
        pass

    r = ThreadRouter(schedule_save=schedule_save, has_window_state=lambda _w: False)
    r.bind_channel("C0BOUND", "@7", window_name="api")
    install_thread_router(r)
    return r


def test_empty_user_id_never_authorized(bound_router):
    assert is_authorized("", "C0BOUND") is False
    assert is_authorized("", "") is False


def test_allowed_user_in_meta_channel(bound_router, monkeypatch):
    monkeypatch.setenv("ALLOWED_USERS", "U0ALLOWED")
    from ccslack.config import config as _cfg

    monkeypatch.setattr(_cfg, "allowed_users", {"U0ALLOWED"})
    assert is_authorized("U0ALLOWED", "C0META_NOT_BOUND") is True


def test_non_allowed_user_in_meta_channel(bound_router, monkeypatch):
    from ccslack.config import config as _cfg

    monkeypatch.setattr(_cfg, "allowed_users", {"U0ALLOWED"})
    assert is_authorized("U0OUTSIDER", "C0META_NOT_BOUND") is False


def test_bound_channel_member_passes_without_global_allow(bound_router, monkeypatch):
    """The key new behaviour — a user not on the global list is trusted as long
    as the event came from a channel we've bound to a session."""
    from ccslack.config import config as _cfg

    monkeypatch.setattr(_cfg, "allowed_users", {"U0ALLOWED"})
    assert is_authorized("U0OUTSIDER", "C0BOUND") is True


def test_unbound_channel_falls_back_to_allow_list(bound_router, monkeypatch):
    from ccslack.config import config as _cfg

    monkeypatch.setattr(_cfg, "allowed_users", {"U0ALLOWED"})
    assert is_authorized("U0OUTSIDER", "C0OTHER") is False
    assert is_authorized("U0ALLOWED", "C0OTHER") is True


def test_is_meta_authorized_never_trusts_channel(bound_router, monkeypatch):
    from ccslack.config import config as _cfg

    monkeypatch.setattr(_cfg, "allowed_users", {"U0ALLOWED"})
    # Even from inside a bound channel, meta_authorized only honours the list.
    assert is_meta_authorized("U0OUTSIDER") is False
    assert is_meta_authorized("U0ALLOWED") is True
    assert is_meta_authorized("") is False


def test_is_dm_channel_prefix():
    assert is_dm_channel("D0123ABC") is True
    assert is_dm_channel("C0METATEST") is False
    assert is_dm_channel("") is False


def test_meta_surface_channel_mode(monkeypatch):
    from ccslack.config import config as _cfg

    monkeypatch.setattr(_cfg, "meta_channel_id", "C0METATEST")
    monkeypatch.setattr(_cfg, "meta_surface", "channel")
    assert is_meta_surface("C0METATEST") is True
    assert is_meta_surface("D0DM") is False
    assert is_meta_surface("C0OTHER") is False
    assert is_meta_surface("") is False


def test_meta_surface_hybrid_mode(monkeypatch):
    from ccslack.config import config as _cfg

    monkeypatch.setattr(_cfg, "meta_channel_id", "C0METATEST")
    monkeypatch.setattr(_cfg, "meta_surface", "hybrid")
    assert is_meta_surface("C0METATEST") is True
    assert is_meta_surface("D0DM") is True
    assert is_meta_surface("C0OTHER") is False


def test_meta_surface_dm_mode_excludes_meta_channel(monkeypatch):
    from ccslack.config import config as _cfg

    monkeypatch.setattr(_cfg, "meta_channel_id", "C0METATEST")
    monkeypatch.setattr(_cfg, "meta_surface", "dm")
    assert is_meta_surface("D0DM") is True
    assert is_meta_surface("C0METATEST") is False
