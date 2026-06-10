import pytest
from slack_sdk.errors import SlackApiError

from ccslack.handlers.meta import (
    _CHANNEL_NAME_MAX_LEN,
    _channel_name_candidates,
    _create_unique_channel,
)


class _FakeBolt:
    def __init__(self, taken: set[str], hard_error: str | None = None) -> None:
        self.taken = set(taken)
        self.hard_error = hard_error
        self.attempts: list[str] = []

    async def conversations_create(self, *, name: str, is_private: bool):  # noqa: ARG002
        self.attempts.append(name)
        if self.hard_error:
            raise SlackApiError(self.hard_error, {"error": self.hard_error})
        if name in self.taken:
            raise SlackApiError("name_taken", {"error": "name_taken"})
        return {"channel": {"id": f"C_{name}"}}


def test_candidates_order():
    cands = _channel_name_candidates("ccslack-vrender", "@9")
    assert cands[:3] == ["ccslack-vrender", "ccslack-vrender-9", "ccslack-vrender-2"]


def test_candidates_dedup_window_id_and_counter():
    # window id "@2" collides with the -2 counter; it must appear once.
    cands = _channel_name_candidates("ccslack-x", "@2")
    assert cands.count("ccslack-x-2") == 1


def test_candidates_respect_length_cap():
    base = "a" * 90
    for name in _channel_name_candidates(base, "@123"):
        assert len(name) <= _CHANNEL_NAME_MAX_LEN


@pytest.mark.asyncio
async def test_create_unique_first_name_free():
    bolt = _FakeBolt(taken=set())
    channel_id, err = await _create_unique_channel(bolt, "ccslack-vrender", "@9")
    assert channel_id == "C_ccslack-vrender"
    assert err == ""
    assert bolt.attempts == ["ccslack-vrender"]


@pytest.mark.asyncio
async def test_create_unique_walks_past_taken_names():
    bolt = _FakeBolt(taken={"ccslack-vrender", "ccslack-vrender-9"})
    channel_id, err = await _create_unique_channel(bolt, "ccslack-vrender", "@9")
    assert channel_id == "C_ccslack-vrender-2"
    assert err == ""
    assert bolt.attempts == ["ccslack-vrender", "ccslack-vrender-9", "ccslack-vrender-2"]


@pytest.mark.asyncio
async def test_create_unique_aborts_on_non_name_taken_error():
    bolt = _FakeBolt(taken=set(), hard_error="restricted_action")
    channel_id, err = await _create_unique_channel(bolt, "ccslack-vrender", "@9")
    assert channel_id is None
    assert err == "restricted_action"
    assert bolt.attempts == ["ccslack-vrender"]  # no further probing


@pytest.mark.asyncio
async def test_create_unique_gives_up_when_all_taken():
    bolt = _FakeBolt(taken=set(_channel_name_candidates("ccslack-vrender", "@9")))
    channel_id, err = await _create_unique_channel(bolt, "ccslack-vrender", "@9")
    assert channel_id is None
    assert err == "name_taken"
