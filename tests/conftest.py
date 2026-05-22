"""Shared pytest fixtures for ccslack tests.

Sets the env vars that ``ccslack.config.Config`` requires before any test
module imports ``ccslack.*``. Tests can override ``CCSLACK_DIR`` per-test via
the ``ccslack_dir`` fixture for isolation.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

import pytest


def _set_stub_env() -> None:
    os.environ.setdefault("SLACK_BOT_TOKEN", "xoxb-test")
    os.environ.setdefault("SLACK_APP_TOKEN", "xapp-test")
    os.environ.setdefault("SLACK_META_CHANNEL_ID", "C0METATEST")
    os.environ.setdefault("ALLOWED_USERS", "U0TESTUSER")


_set_stub_env()


@pytest.fixture
def ccslack_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """Per-test config dir; ensures state.json doesn't leak across tests."""
    monkeypatch.setenv("CCSLACK_DIR", str(tmp_path))
    yield tmp_path
