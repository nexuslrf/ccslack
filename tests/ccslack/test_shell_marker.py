from ccslack.handlers import shell_marker
from ccslack.handlers.shell_marker import (
    _command_from_echo,
    _extract_command_output,
    _extract_passive_output,
    _find_command_echo,
    _find_in_progress,
    _format,
    _has_markers_in_tail,
    strip_terminal_glyphs,
)


def _wrap(prompt: str, exit_code: int = 0, suffix: str = "") -> str:
    """Build a wrap-mode marker line: ``<prompt>⌘<n>⌘ <suffix>``."""
    return f"{prompt}⌘{exit_code}⌘ {suffix}".rstrip()


def test_strip_terminal_glyphs_removes_marker_and_ansi():
    text = "before \x1b[2m⌘0⌘ \x1b[0mafter"
    assert strip_terminal_glyphs(text) == "before after"


def test_has_markers_in_tail_positive():
    text = "\n".join(["unrelated", "older", _wrap("(base)$ ")])
    assert _has_markers_in_tail(text) is True


def test_has_markers_in_tail_negative_when_only_top_lines_match():
    head = _wrap("(base)$ ")
    body = "\n".join(["filler"] * 30)
    text = head + "\n" + body
    assert _has_markers_in_tail(text) is False


def test_extract_command_output_slices_between_markers():
    text = "\n".join(
        [
            _wrap("(base)$ ", suffix="ls -la"),
            "file1",
            "file2",
            "file3",
            _wrap("(base)$ "),
        ]
    )
    out = _extract_command_output(text)
    assert out.text == "file1\nfile2\nfile3"
    assert out.exit_code == 0


def test_extract_command_output_captures_nonzero_exit():
    text = "\n".join(
        [
            _wrap("(base)$ ", exit_code=0, suffix="false"),
            _wrap("(base)$ ", exit_code=1),
        ]
    )
    out = _extract_command_output(text)
    assert out.exit_code == 1


def test_extract_command_output_returns_empty_when_no_bare_marker():
    # In-progress: command echoed but no trailing bare prompt.
    text = "\n".join(
        [
            _wrap("(base)$ ", suffix="sleep 5"),
            "(still going…)",
        ]
    )
    assert _extract_command_output(text).text == ""


def test_find_command_echo_walks_upward_from_bare_prompt():
    lines = [
        "noise",
        _wrap("(base)$ ", suffix="ls"),
        "out",
        _wrap("(base)$ "),
    ]
    echo = _find_command_echo(lines)
    assert echo is not None
    label, idx = echo
    assert "ls" in label
    assert idx == 1


def test_find_in_progress_returns_streaming_output():
    lines = [
        "noise",
        _wrap("(base)$ ", suffix="du -sh *"),
        "1.3T cache",
        "424G datasets",
    ]
    passive = _find_in_progress(lines)
    assert passive is not None
    assert "du -sh" in passive.command_echo
    assert passive.text == "1.3T cache\n424G datasets"
    assert passive.exit_code is None


def test_extract_passive_output_completed_command():
    text = "\n".join(
        [
            _wrap("(base)$ ", exit_code=0, suffix="ls"),
            "file1",
            "file2",
            _wrap("(base)$ ", exit_code=0),
        ]
    )
    passive = _extract_passive_output(text)
    assert passive is not None
    assert passive.text == "file1\nfile2"
    assert passive.exit_code == 0
    assert "ls" in passive.command_echo


def test_extract_passive_output_in_progress():
    text = "\n".join(
        [
            _wrap("(base)$ ", suffix="du -sh *"),
            "1.3T cache",
            "424G datasets",
        ]
    )
    passive = _extract_passive_output(text)
    assert passive is not None
    assert passive.text == "1.3T cache\n424G datasets"
    assert passive.exit_code is None  # still running


def test_extract_passive_output_returns_none_for_idle_shell():
    # Just a bare prompt — no preceding command echo at all.
    text = _wrap("(base)$ ")
    assert _extract_passive_output(text) is None


def test_command_from_echo_strips_prompt_and_marker():
    echo = _wrap("(base)$ ", suffix="ls -la")
    assert _command_from_echo(echo) == "ls -la"


def test_format_running_appends_marker():
    body = _format("du -sh *", "1.3T cache", in_progress=True)
    assert "> `du -sh *`" in body
    assert "(running…)" in body
    assert "1.3T cache" in body


def test_format_completed_no_marker():
    body = _format("ls", "file1\nfile2", in_progress=False)
    assert body == "> `ls`\n```\nfile1\nfile2\n```"


def test_format_no_command_no_output():
    assert _format("", "", in_progress=False) == ""


def test_clear_window_drops_state():
    shell_marker._state["@99"] = shell_marker._ShellMonitorState(
        last_command_echo="prev", msg_ts="1234.5"
    )
    shell_marker.clear_window("@99")
    assert "@99" not in shell_marker._state


def test_mark_slack_command_records_message_ts():
    shell_marker.reset_for_testing()
    shell_marker.mark_slack_command("@77", slack_user_message_ts="1700000000.000001")
    state = shell_marker._state["@77"]
    assert state.slack_user_message_ts == "1700000000.000001"
    shell_marker.reset_for_testing()
