import pytest

from ccslack.thread_router import ThreadRouter


@pytest.fixture
def router() -> ThreadRouter:
    saves: list[int] = []
    states: set[str] = set()

    def schedule_save() -> None:
        saves.append(1)

    def has_window(window_id: str) -> bool:
        return window_id in states

    r = ThreadRouter(schedule_save=schedule_save, has_window_state=has_window)
    r._test_states = states  # type: ignore[attr-defined] — convenience for tests
    return r


def test_bind_records_channel_and_display_name(router: ThreadRouter) -> None:
    router.bind_channel("C123", "@0", window_name="api")
    assert router.channel_bindings == {"C123": "@0"}
    assert router.get_window_for_channel("C123") == "@0"
    assert router.get_channel_for_window("@0") == "C123"
    assert router.get_display_name("@0") == "api"
    assert router.has_window("@0") is True


def test_effective_window_id_prefers_binding_over_fallback(
    router: ThreadRouter,
) -> None:
    router.bind_channel("C123", "@9", window_name="api")
    # Stale button value (@1) is ignored in favour of the live binding.
    assert router.effective_window_id("C123", "@1") == "@9"


def test_effective_window_id_uses_fallback_when_unbound(router: ThreadRouter) -> None:
    assert router.effective_window_id("C404", "@1") == "@1"
    assert router.effective_window_id("C404") == ""


def test_chat_thread_mark_and_query(router: ThreadRouter) -> None:
    router.mark_chat_thread("C1", "100.1")
    assert router.is_chat_thread("C1", "100.1") is True
    assert router.is_chat_thread("C1", "999.9") is False
    assert router.is_chat_thread("C2", "100.1") is False


def test_chat_thread_persists_roundtrip(router: ThreadRouter) -> None:
    router.mark_chat_thread("C1", "100.1")
    router.mark_chat_thread("C1", "100.2")
    data = router.to_dict()

    other = ThreadRouter(schedule_save=lambda: None, has_window_state=lambda _w: False)
    other.from_dict(data)
    assert other.is_chat_thread("C1", "100.1") is True
    assert other.is_chat_thread("C1", "100.2") is True


def test_unbind_preserves_chat_threads(router: ThreadRouter) -> None:
    # unbind is also used to rebind on restore/resume — the channel and its
    # chat threads must survive. Only explicit teardown forgets them.
    router.bind_channel("C1", "@1")
    router.mark_chat_thread("C1", "100.1")
    router.unbind_channel("C1")
    assert router.is_chat_thread("C1", "100.1") is True


def test_chat_thread_survives_rebind(router: ThreadRouter) -> None:
    # Simulate a restore: unbind the dead window, then bind a fresh one.
    router.bind_channel("C1", "@1")
    router.mark_chat_thread("C1", "100.1")
    router.unbind_channel("C1")
    router.bind_channel("C1", "@2")
    assert router.is_chat_thread("C1", "100.1") is True


def test_clear_chat_threads_forgets_on_teardown(router: ThreadRouter) -> None:
    router.bind_channel("C1", "@1")
    router.mark_chat_thread("C1", "100.1")
    router.clear_chat_threads("C1")
    assert router.is_chat_thread("C1", "100.1") is False


def test_reset_clears_chat_threads(router: ThreadRouter) -> None:
    router.mark_chat_thread("C1", "100.1")
    router.reset()
    assert router.is_chat_thread("C1", "100.1") is False


def test_rebind_evicts_old_binding(router: ThreadRouter) -> None:
    router.bind_channel("C111", "@0")
    router.bind_channel("C222", "@0")
    # Old channel must be evicted to maintain 1 channel = 1 window.
    assert router.get_window_for_channel("C111") is None
    assert router.get_window_for_channel("C222") == "@0"


def test_unbind_clears_reverse_index(router: ThreadRouter) -> None:
    router.bind_channel("C123", "@0", window_name="api")
    old = router.unbind_channel("C123")
    assert old == "@0"
    assert router.get_window_for_channel("C123") is None
    assert router.get_channel_for_window("@0") is None


def test_to_from_dict_roundtrip(router: ThreadRouter) -> None:
    router.bind_channel("C123", "@0", window_name="api")
    router.bind_channel("C456", "@5", window_name="ui")
    data = router.to_dict()

    saves: list[int] = []

    def schedule_save() -> None:
        saves.append(1)

    other = ThreadRouter(
        schedule_save=schedule_save, has_window_state=lambda _wid: False
    )
    other.from_dict(data)
    assert other.channel_bindings == {"C123": "@0", "C456": "@5"}
    assert other.get_display_name("@0") == "api"
    assert saves == []  # from_dict must NOT trigger a save


def test_iter_channel_bindings_yields_pairs(router: ThreadRouter) -> None:
    router.bind_channel("C111", "@0")
    router.bind_channel("C222", "@5")
    pairs = sorted(router.iter_channel_bindings())
    assert pairs == [("C111", "@0"), ("C222", "@5")]
