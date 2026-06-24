"""Shared pytest fixtures for ccslack tests.

Sets the env vars that ``ccslack.config.Config`` requires before any test
module imports ``ccslack.*``.

CRITICAL: ``CCSLACK_DIR`` is pointed at a throwaway temp directory *here, at
collection time*, before any ``ccslack`` import. ``ccslack.config.config`` and
``ccslack.session.session_manager`` are module-level singletons built on first
import — ``session_manager`` captures ``config.state_file`` in its
``StatePersistence``. Tests routinely mutate those singletons (``bind_channel``,
``set_window_provider``, …), each of which schedules a debounced save. Without
redirecting ``CCSLACK_DIR`` those saves clobber the developer's real
``~/.ccslack/state.json`` (real session bindings → test fixtures). The temp dir
keeps every test write isolated to a per-run scratch directory.
"""

from __future__ import annotations

import os
import tempfile
from collections.abc import Iterator
from pathlib import Path

import pytest

# Per-test-run scratch config dir. Created and exported before any ccslack
# import so the config singleton never resolves to the real ~/.ccslack.
_TEST_CONFIG_DIR = tempfile.mkdtemp(prefix="ccslack-test-")


def _set_stub_env() -> None:
    # Force (not setdefault) the config dir so a stray CCSLACK_DIR in the
    # developer's shell can't redirect test writes back to a real directory.
    os.environ["CCSLACK_DIR"] = _TEST_CONFIG_DIR
    os.environ.setdefault("SLACK_BOT_TOKEN", "xoxb-test")
    os.environ.setdefault("SLACK_APP_TOKEN", "xapp-test")
    os.environ.setdefault("SLACK_META_CHANNEL_ID", "C0METATEST")
    os.environ.setdefault("ALLOWED_USERS", "U0TESTUSER")


_set_stub_env()


@pytest.fixture(autouse=True)
def _isolate_singleton_state() -> Iterator[None]:
    """Reset the shared in-memory singleton state around every test.

    Many tests mutate the module-level ``session_manager`` (and the stores /
    router it owns). Clearing before and after each test stops fixtures from
    one test leaking bindings / window states into another — and, combined
    with the temp ``CCSLACK_DIR`` above, stops any of it reaching disk that
    matters.
    """
    # Lazy: importing here (not at module top) lets the env above take effect
    # before ccslack.config is first imported. Importing ``session`` constructs
    # the ``session_manager`` singleton, which wires the store/router proxies —
    # otherwise accessing them below raises "not yet wired" for tests that only
    # imported ``config``.
    import ccslack.session  # noqa: F401  (import for side effect: wires proxies)
    from ccslack import fleet_state
    from ccslack.thread_router import thread_router
    from ccslack.user_preferences import user_preferences
    from ccslack.window_state_store import window_store

    def _clear() -> None:
        window_store.window_states.clear()
        thread_router.channel_bindings.clear()
        thread_router.window_display_names.clear()
        thread_router._window_to_channel.clear()
        user_preferences.user_window_offsets.clear()
        user_preferences.user_dir_favorites.clear()
        fleet_state.reset()

    _clear()
    yield
    _clear()


@pytest.fixture
def ccslack_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """Per-test config dir; ensures state.json doesn't leak across tests."""
    monkeypatch.setenv("CCSLACK_DIR", str(tmp_path))
    yield tmp_path
