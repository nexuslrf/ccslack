from ccslack.handlers.polling.prompt_probe import _looks_like_prompt


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
