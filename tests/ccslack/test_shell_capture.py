from ccslack.handlers.shell_capture import (
    _diff_new_lines,
    _format_body,
    _looks_like_prompt,
    _strip_echoed_command,
    _strip_trailing_prompt,
)


def test_diff_returns_suffix():
    before = "line1\nline2"
    after = "line1\nline2\nline3"
    assert _diff_new_lines(before, after) == "\nline3"


def test_diff_falls_back_when_snapshot_rotated():
    after = "\n".join(f"l{i}" for i in range(30))
    out = _diff_new_lines("unrelated", after)
    # Falls back to the last 20 lines.
    assert out.splitlines() == [f"l{i}" for i in range(10, 30)]


def test_looks_like_prompt_typical_bash():
    assert _looks_like_prompt("(base) ruofan@amd-03:/nvmepool/ruofan$") is True


def test_looks_like_prompt_zsh_arrow():
    assert _looks_like_prompt("~/code ❯") is True


def test_looks_like_prompt_rejects_user_output():
    # Coincidentally ending in '$' but no path-ish hint.
    assert _looks_like_prompt("got 42 #issues") is False
    assert _looks_like_prompt("price: $100") is False


def test_strip_trailing_prompt_removes_bash_prompt():
    text = (
        "ls\n"
        "file1 file2\n"
        "(base) ruofan@amd-03:/nvmepool/ruofan$"
    )
    assert _strip_trailing_prompt(text) == "ls\nfile1 file2"


def test_strip_trailing_prompt_keeps_real_output():
    text = "ls\nfile1\nfile2"
    assert _strip_trailing_prompt(text) == "ls\nfile1\nfile2"


def test_strip_echoed_command_drops_first_match():
    text = "ls\nfile1\nfile2"
    assert _strip_echoed_command(text, "ls") == "file1\nfile2"


def test_strip_echoed_command_leaves_unrelated_first_line():
    text = "info: started\nfile1"
    assert _strip_echoed_command(text, "ls") == "info: started\nfile1"


def test_strip_echoed_command_with_empty_command():
    assert _strip_echoed_command("ls\nfile1", "") == "ls\nfile1"


def test_format_body_command_with_output():
    body = _format_body("ls", "file1\nfile2")
    assert body.startswith("> `ls`")
    assert "```\nfile1\nfile2\n```" in body


def test_format_body_command_no_output():
    body = _format_body("clear", "")
    assert body == "> `clear` _(no output)_"


def test_format_body_no_command_no_output_is_empty():
    assert _format_body("", "") == ""


def test_end_to_end_strip_pipeline():
    # Reproduces the user-reported pane after `ls`.
    captured = (
        "ls\n"
        "file1 file2 file3\n"
        "file4 file5\n"
        "(base) ruofan@amd-03:/nvmepool/ruofan/projects_leo$"
    )
    cleaned = _strip_trailing_prompt(captured)
    cleaned = _strip_echoed_command(cleaned, "ls").rstrip()
    body = _format_body("ls", cleaned)
    expected = (
        "> `ls`\n"
        "```\n"
        "file1 file2 file3\n"
        "file4 file5\n"
        "```"
    )
    assert body == expected
