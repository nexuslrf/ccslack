import pytest

from ccslack.handlers.meta import _resolve_kill_target
from ccslack.thread_router import ThreadRouter, install_thread_router


@pytest.fixture
def wired_router() -> ThreadRouter:
    saves: list[int] = []

    def schedule_save() -> None:
        saves.append(1)

    r = ThreadRouter(schedule_save=schedule_save, has_window_state=lambda _w: False)
    r.bind_channel("C0BCDEFG", "@5", window_name="api")
    install_thread_router(r)
    return r


def test_empty_arg_uses_from_channel(wired_router: ThreadRouter) -> None:
    assert _resolve_kill_target("", from_channel="C0BCDEFG") == ("C0BCDEFG", "@5")


def test_empty_arg_unbound_channel_returns_none(wired_router: ThreadRouter) -> None:
    assert _resolve_kill_target("", from_channel="C0NOSUCH") is None


def test_channel_mention(wired_router: ThreadRouter) -> None:
    assert _resolve_kill_target("<#C0BCDEFG|api>", from_channel="C0META") == (
        "C0BCDEFG",
        "@5",
    )


def test_bare_channel_id(wired_router: ThreadRouter) -> None:
    assert _resolve_kill_target("C0BCDEFG", from_channel="C0META") == (
        "C0BCDEFG",
        "@5",
    )


def test_window_id(wired_router: ThreadRouter) -> None:
    assert _resolve_kill_target("@5", from_channel="C0META") == ("C0BCDEFG", "@5")


def test_unknown_window_id_returns_none(wired_router: ThreadRouter) -> None:
    assert _resolve_kill_target("@999", from_channel="C0META") is None


def test_gibberish_returns_none(wired_router: ThreadRouter) -> None:
    assert _resolve_kill_target("not-a-thing", from_channel="C0META") is None
