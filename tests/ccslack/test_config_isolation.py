import os
from pathlib import Path

from ccslack.config import config


def test_config_dir_is_a_temp_dir_not_real_home():
    """Regression guard: the test config dir must NOT be the developer's real
    ~/.ccslack. A bug in conftest once let singleton saves (bind_channel,
    set_window_provider, …) clobber the real state.json with test fixtures."""
    real_home = Path.home() / ".ccslack"
    assert config.config_dir != real_home
    assert str(config.config_dir).startswith(
        os.path.realpath("/tmp")
    ) or "ccslack-test-" in str(config.config_dir), (
        f"unexpected test config dir: {config.config_dir}"
    )


def test_ccslack_dir_env_points_at_scratch():
    val = os.environ.get("CCSLACK_DIR", "")
    assert "ccslack-test-" in val, f"CCSLACK_DIR not redirected: {val!r}"
