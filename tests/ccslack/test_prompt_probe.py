import asyncio
import time
from unittest.mock import patch

import pytest

from ccslack.handlers.polling import prompt_probe
from ccslack.handlers.polling.prompt_probe import (
    DISMISS_COOLDOWN_SECONDS,
    _looks_like_prompt,
    clear_dismiss,
    mark_dismissed,
)
from ccslack.slack_client import FakeSlackClient


def test_detects_codex_picker_with_selector_and_numbered_list():
    tail = "\n".join(
        [
            "Allow this command?",
            "❯ 1. Yes",
            "  2. No",
            "  3. Yes, and approve always",
        ]
    )
    assert _looks_like_prompt(tail) is True


def test_detects_codex_exec_approval_with_angle_quote():
    # Reproduces the real Codex approval prompt seen in the field.
    tail = "\n".join(
        [
            "Would you like to run the following command?",
            "  Reason: The sandbox previously failed to start for `ls`.",
            "  $ ls",
            "› 1. Yes, proceed (y)",
            "  2. Yes, and don't ask again for commands that start with `ls` (p)",
            "  3. No, and tell Codex what to do differently (esc)",
        ]
    )
    assert _looks_like_prompt(tail) is True


def test_detects_yn_inline():
    tail = "Run git push origin main? [y/N]"
    assert _looks_like_prompt(tail) is True


def test_rejects_regular_output():
    tail = "build finished in 4.2s\n  - 12 files emitted\n  - 0 errors"
    assert _looks_like_prompt(tail) is False


def test_rejects_numbered_list_without_selector():
    tail = "Here are three things:\n1. first\n2. second\n3. third"
    # Selector arrow missing → must not match. y/n absent too.
    assert _looks_like_prompt(tail) is False


def test_mark_dismissed_sets_cooldown():
    clear_dismiss("C0X")
    before = time.monotonic()
    mark_dismissed("C0X")
    until = prompt_probe._dismissed_until.get("C0X", 0.0)
    # The cooldown window should land within the expected range.
    assert before + DISMISS_COOLDOWN_SECONDS - 1 <= until <= before + DISMISS_COOLDOWN_SECONDS + 1
    clear_dismiss("C0X")


def test_clear_dismiss_removes_cooldown():
    mark_dismissed("C0Y")
    assert "C0Y" in prompt_probe._dismissed_until
    clear_dismiss("C0Y")
    assert "C0Y" not in prompt_probe._dismissed_until


def test_dismiss_cooldown_constant_is_reasonable():
    # Sanity-check we haven't accidentally set this to something absurd.
    assert 5.0 <= DISMISS_COOLDOWN_SECONDS <= 300.0


@pytest.mark.asyncio
async def test_dismiss_cooldown_self_extends_while_prompt_present():
    """Regression for the bug where the picker reposted exactly 30 s after
    a Dismiss because the Codex prompt was still on-screen and the pane
    micro-changed during the cooldown window. Self-extension keeps the
    cooldown alive until ``_looks_like_prompt`` returns False."""
    prompt_probe._dismissed_until.clear()
    prompt_probe._last_prompt_hash.clear()

    channel_id = "C0X"
    window_id = "@1"

    # Pretend a real prompt is up.
    tail = "› 1. Yes, proceed (y)\n  2. No\n  3. Yes, always"

    client = FakeSlackClient()

    async def fake_capture(_wid, history=0, with_ansi=False):  # noqa: ANN001
        return tail

    async def noop_enter(*args, **kwargs):  # noqa: ANN001
        raise AssertionError("enter_from_pane should not be called while dismissed")

    # Seed dismissal.
    mark_dismissed(channel_id)
    initial_until = prompt_probe._dismissed_until[channel_id]
    # Make the cooldown look like it's about to expire.
    prompt_probe._dismissed_until[channel_id] = time.monotonic() + 0.05

    with (
        patch.object(
            prompt_probe.session_manager, "view_window", return_value=type(
                "V", (), {"provider_name": "codex"}
            )(),
        ),
        patch.object(
            prompt_probe.tmux_manager, "capture_pane_scrollback", side_effect=fake_capture
        ),
        patch("ccslack.handlers.interactive.is_in_interactive_mode", return_value=False),
        patch("ccslack.handlers.interactive.enter_from_pane", side_effect=noop_enter),
    ):
        await prompt_probe.maybe_post_prompt(client, channel_id, window_id)

    # The tick should NOT have posted a picker and should have *extended*
    # the cooldown to a fresh DISMISS_COOLDOWN_SECONDS window.
    extended_until = prompt_probe._dismissed_until.get(channel_id, 0.0)
    assert extended_until > initial_until - 1.0  # strictly later than original setpoint
    assert client.call_count("chat_postMessage") == 0

    prompt_probe._dismissed_until.clear()
    prompt_probe._last_prompt_hash.clear()


@pytest.mark.asyncio
async def test_dismiss_cooldown_clears_when_prompt_goes_away():
    """When ``_looks_like_prompt`` returns False (agent moved on) the
    cooldown is cleared so a *next* prompt can fire a fresh picker."""
    prompt_probe._dismissed_until.clear()
    prompt_probe._last_prompt_hash.clear()

    channel_id = "C0Y"
    window_id = "@2"
    mark_dismissed(channel_id)

    # Capture returns content that is NOT a prompt anymore.
    async def fake_capture(_wid, history=0, with_ansi=False):  # noqa: ANN001
        return "ordinary command output\nno prompt here\n"

    client = FakeSlackClient()
    with (
        patch.object(
            prompt_probe.session_manager,
            "view_window",
            return_value=type("V", (), {"provider_name": "codex"})(),
        ),
        patch.object(
            prompt_probe.tmux_manager,
            "capture_pane_scrollback",
            side_effect=fake_capture,
        ),
        patch("ccslack.handlers.interactive.is_in_interactive_mode", return_value=False),
    ):
        await prompt_probe.maybe_post_prompt(client, channel_id, window_id)

    assert channel_id not in prompt_probe._dismissed_until
    asyncio.get_event_loop()  # quiet pytest-asyncio fixture warnings
    prompt_probe._last_prompt_hash.clear()
