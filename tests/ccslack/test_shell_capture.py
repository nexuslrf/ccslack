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


def test_diff_finds_before_as_substring_when_window_slides():
    # tmux's `-S -N` window slides as new output streams in; after may
    # contain extra prefix lines that weren't in before.
    before = "(base) ruofan@host:/path$ "
    after = (
        "earlier-line-A\nearlier-line-B\n"
        "(base) ruofan@host:/path$ \n"
        "rocm-utils\n+--+\n| GPU[0] |\n+--+"
    )
    out = _diff_new_lines(before, after)
    assert out.startswith("\nrocm-utils")
    assert "GPU[0]" in out
    # Crucially: header lines from the output are not dropped.
    assert "+--+" in out


def test_diff_uses_command_anchor_when_prefix_missing():
    # If neither startswith nor rfind matches (e.g. terminal redraw munged
    # the prompt line), fall back to slicing after the command echo.
    before = "(base) prompt$ "
    after = "(redrawn prompt)\nrocm-utils\noutput-line-1\noutput-line-2"
    out = _diff_new_lines(before, after, command="rocm-utils")
    assert out.strip().startswith("output-line-1")


def test_diff_fallback_tail_is_50_lines_not_20():
    # 30-line output should be returned in full — the fix is that the
    # fallback was bumped from 20 → 50.
    after = "\n".join(f"l{i}" for i in range(30))
    out = _diff_new_lines("unrelated content", after, command="")
    # All 30 lines preserved (well under the 50 cap).
    assert out.splitlines() == [f"l{i}" for i in range(30)]


def test_diff_fallback_caps_at_50_lines_on_huge_rotation():
    after = "\n".join(f"l{i}" for i in range(120))
    out = _diff_new_lines("unrelated content", after, command="")
    # Bottom 50 retained.
    assert out.splitlines() == [f"l{i}" for i in range(70, 120)]


def test_diff_rocm_utils_full_table_preserved():
    """End-to-end regression: 8-GPU rocm-utils output should NOT lose its
    header rows when the capture window slid."""
    before = "(base) ruofan@amd-03:/path$ "
    # Pre-send capture had some earlier prompt lines that have since
    # scrolled the window down; after capture starts midway through them.
    earlier_noise = "\n".join(f"old-line-{i}" for i in range(15))
    table = "\n".join(
        [
            "+--------+------------+----------------+-------------+",
            "| ROCm   |   Util (%) | Memory (MiB)   |   Temp (°C) |",
            "+========+============+================+=============+",
            *[
                f"| GPU[{g}] |          0 | 11/65520       |          40 |\n"
                f"+--------+------------+----------------+-------------+"
                for g in range(8)
            ],
        ]
    )
    after = (
        earlier_noise
        + "\n"
        + before
        + "\nrocm-utils\n"
        + table
        + "\n(base) ruofan@amd-03:/path$"
    )
    out = _diff_new_lines(before, after, command="rocm-utils")
    # The full header must survive.
    assert "| ROCm" in out
    assert "GPU[0]" in out
    assert "GPU[7]" in out


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
