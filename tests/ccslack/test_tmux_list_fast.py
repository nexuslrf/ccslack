"""list_windows / list_panes use one minimal ``tmux list-panes -F`` call.

These are the per-second polling hot paths. They must not fall back to
libtmux object enumeration, which expands ``#{user}`` (a getpwuid() inside
the tmux server) for every row.
"""

import pytest

from ccslack import tmux_manager as tm
from ccslack.config import config

SEP = tm._FIELD_SEP


class _Proc:
    def __init__(self, stdout: list[str], stderr: list[str] | None = None) -> None:
        self.stdout = stdout
        self.stderr = stderr or []


class _FakeServer:
    def __init__(self, proc: _Proc) -> None:
        self.proc = proc
        self.calls: list[tuple] = []

    def cmd(self, *args):  # noqa: ANN002
        self.calls.append(args)
        return self.proc


def _row(*values: str) -> str:
    return SEP.join(values)


def _manager(monkeypatch, proc: _Proc) -> tuple[tm.TmuxManager, _FakeServer]:
    mgr = tm.TmuxManager(session_name="ccslack")
    server = _FakeServer(proc)
    monkeypatch.setattr(mgr, "_server", server)
    return mgr, server


@pytest.mark.asyncio
async def test_list_windows_single_call_minimal_format(monkeypatch):
    monkeypatch.setattr(config, "own_window_id", "@9")
    proc = _Proc(
        [
            _row(
                "ccslack",
                "@0",
                "__main__",
                "1",
                "/home/u",
                "bash",
                "/dev/pts/0",
                "80",
                "24",
            ),
            # Two panes in @1; only the active one should be reported.
            _row(
                "ccslack",
                "@1",
                "proj",
                "0",
                "/home/u/other",
                "bash",
                "/dev/pts/1",
                "10",
                "10",
            ),
            _row(
                "ccslack",
                "@1",
                "proj",
                "1",
                "/home/u/proj",
                "claude",
                "/dev/pts/2",
                "188",
                "50",
            ),
            _row(
                "ccslack", "@2", "_hidden", "1", "/x", "bash", "/dev/pts/3", "80", "24"
            ),
            _row("ccslack", "@9", "self", "1", "/x", "bash", "/dev/pts/4", "80", "24"),
            _row(
                "ccslack",
                "@3",
                "codex-win",
                "1",
                "/y",
                "codex",
                "/dev/pts/5",
                "bad",
                "32",
            ),
        ]
    )
    mgr, server = _manager(monkeypatch, proc)

    windows = await mgr.list_windows()

    assert len(server.calls) == 1
    call = server.calls[0]
    assert call[:4] == ("list-panes", "-s", "-t", "ccslack")
    fmt = call[-1]
    assert "#{user}" not in fmt
    assert "#{pane_current_command}" in fmt

    assert [w.window_id for w in windows] == ["@1", "@3"]
    w1 = windows[0]
    assert w1.window_name == "proj"
    assert w1.cwd == "/home/u/proj"
    assert w1.pane_current_command == "claude"
    assert w1.pane_tty == "/dev/pts/2"
    assert (w1.pane_width, w1.pane_height) == (188, 50)
    assert windows[1].pane_width == 0  # unparsable width degrades to 0


@pytest.mark.asyncio
async def test_list_windows_missing_session_is_empty(monkeypatch):
    mgr, _ = _manager(monkeypatch, _Proc([], ["can't find session: ccslack"]))
    assert await mgr.list_windows() == []


@pytest.mark.asyncio
async def test_list_windows_ignores_malformed_rows(monkeypatch):
    mgr, _ = _manager(monkeypatch, _Proc(["garbage", _row("ccslack", "@1")]))
    assert await mgr.list_windows() == []


@pytest.mark.asyncio
async def test_list_panes_scoped_to_session(monkeypatch):
    proc = _Proc(
        [
            _row("ccslack", "%1", "0", "0", "bash", "/a", "80", "24"),
            _row("ccslack", "%2", "1", "1", "claude", "/b", "100", "30"),
            _row("other", "%3", "0", "1", "vim", "/c", "80", "24"),
        ]
    )
    mgr, server = _manager(monkeypatch, proc)

    panes = await mgr.list_panes("@1")

    assert server.calls[0][:3] == ("list-panes", "-t", "@1")
    assert "#{user}" not in server.calls[0][-1]
    assert [p.pane_id for p in panes] == ["%1", "%2"]
    assert panes[1].active is True
    assert panes[1].command == "claude"
    assert (panes[1].index, panes[1].width, panes[1].height) == (1, 100, 30)


@pytest.mark.asyncio
async def test_list_panes_missing_window_is_empty(monkeypatch):
    mgr, _ = _manager(monkeypatch, _Proc([], ["can't find window: @77"]))
    assert await mgr.list_panes("@77") == []
