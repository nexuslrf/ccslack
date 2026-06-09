import pytest

from ccslack.tmux_manager import TmuxManager, TmuxWindow


class _FakePane:
    def __init__(self, exit_after: int) -> None:
        self.presses = 0
        self.exit_after = exit_after
        self.sent: list[str] = []

    async def send_keys(self, window_id, text, literal=False, enter=False):  # noqa: ANN001, ARG002
        self.sent.append(text)
        if text == "C-c":
            self.presses += 1
        return True

    async def find(self, window_id):  # noqa: ANN001
        cmd = "bash" if self.presses >= self.exit_after else "node"
        return TmuxWindow(
            window_id=window_id,
            window_name="w",
            cwd="/x",
            pane_current_command=cmd,
        )


def _wire(monkeypatch, fake: _FakePane) -> TmuxManager:
    tm = TmuxManager()
    monkeypatch.setattr(tm, "send_keys", fake.send_keys)
    monkeypatch.setattr(tm, "find_window_by_id", fake.find)
    return tm


@pytest.mark.asyncio
async def test_already_at_shell_sends_no_ctrl_c(monkeypatch):
    fake = _FakePane(exit_after=0)
    tm = _wire(monkeypatch, fake)

    assert await tm.interrupt_agent_to_shell("@1", settle=0, burst_gap=0) is True
    assert fake.sent == []


@pytest.mark.asyncio
async def test_exits_after_rapid_burst(monkeypatch):
    # Claude needs three rapid presses (interrupt -> exit hint -> exit); a
    # single burst should deliver them and reach the shell on the next check.
    fake = _FakePane(exit_after=3)
    tm = _wire(monkeypatch, fake)

    assert (
        await tm.interrupt_agent_to_shell("@1", settle=0, burst=3, burst_gap=0)
        is True
    )
    assert fake.sent == ["C-c", "C-c", "C-c"]
    assert fake.presses >= 3


@pytest.mark.asyncio
async def test_returns_false_when_agent_never_exits(monkeypatch):
    fake = _FakePane(exit_after=999)
    tm = _wire(monkeypatch, fake)

    result = await tm.interrupt_agent_to_shell(
        "@1", max_attempts=2, settle=0, burst=3, burst_gap=0
    )
    assert result is False
    # 2 attempts x 3 presses each.
    assert fake.sent == ["C-c"] * 6


@pytest.mark.asyncio
async def test_returns_false_when_window_dies(monkeypatch):
    tm = TmuxManager()

    async def _find_none(window_id):  # noqa: ANN001, ARG001
        return None

    async def _send_false(window_id, text, literal=False, enter=False):  # noqa: ANN001, ARG001
        return False

    monkeypatch.setattr(tm, "find_window_by_id", _find_none)
    monkeypatch.setattr(tm, "send_keys", _send_false)

    assert (
        await tm.interrupt_agent_to_shell("@1", settle=0, burst_gap=0) is False
    )


@pytest.mark.asyncio
async def test_is_at_shell_true_for_shell_false_for_agent(monkeypatch):
    tm = TmuxManager()

    async def _find(window_id, cmd="bash"):  # noqa: ANN001
        return TmuxWindow(
            window_id=window_id, window_name="w", cwd="/x", pane_current_command=cmd
        )

    monkeypatch.setattr(tm, "find_window_by_id", lambda wid: _find(wid, "zsh"))
    assert await tm.is_at_shell("@1") is True

    monkeypatch.setattr(tm, "find_window_by_id", lambda wid: _find(wid, "node"))
    assert await tm.is_at_shell("@1") is False
