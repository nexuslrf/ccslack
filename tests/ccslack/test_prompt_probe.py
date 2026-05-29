import time

from ccslack.handlers.polling import prompt_probe
from ccslack.handlers.polling.prompt_probe import (
    DISMISS_COOLDOWN_SECONDS,
    _looks_like_prompt,
    clear_dismiss,
    mark_dismissed,
)


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
