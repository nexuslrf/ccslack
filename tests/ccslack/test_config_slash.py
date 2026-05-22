import pytest

from ccslack.config import _resolve_slash_command


def test_resolve_default_adds_leading_slash():
    assert _resolve_slash_command("agent") == "/agent"


def test_resolve_keeps_leading_slash():
    assert _resolve_slash_command("/agent") == "/agent"


def test_resolve_lowercases():
    assert _resolve_slash_command("/Agent") == "/agent"


def test_resolve_allows_underscore_and_hyphen():
    assert _resolve_slash_command("/cc-slack_2") == "/cc-slack_2"


def test_resolve_rejects_spaces():
    with pytest.raises(ValueError, match="invalid"):
        _resolve_slash_command("/has space")


def test_resolve_rejects_empty():
    with pytest.raises(ValueError, match="invalid"):
        _resolve_slash_command("/")


def test_resolve_rejects_too_long():
    with pytest.raises(ValueError, match="invalid"):
        _resolve_slash_command("/" + "x" * 33)
