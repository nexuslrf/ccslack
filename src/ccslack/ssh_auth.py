"""Bridge an SSH tunnel's interactive auth prompt (e.g. Duo 2FA) to Slack.

When ``CCSLACK_SSH_INTERACTIVE`` is on, the router runs each ``ssh`` tunnel under
a pseudo-terminal so its auth prompts — normally answered at the console — can be
captured, posted to the meta channel, and answered from Slack.

Pieces:
  * ``strip_ansi`` / ``looks_like_prompt`` / ``parse_options`` — pure text logic
    for detecting "ssh is waiting for input" and pulling out numbered choices.
  * ``PtyProcess`` — run a subprocess under a PTY, stream its output to a
    callback, and write responses back to it.
  * a host → responder registry so a Slack button/modal can deliver the typed
    answer to the right tunnel.

The text logic is unit-tested; the live PTY path is exercised in the field
against the real prompt (its exact wording varies), which is why the trigger is
a configurable regex (``CCSLACK_SSH_PROMPT_RE``).
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import re
import structlog
from collections.abc import Awaitable, Callable

logger = structlog.get_logger()

_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]|\x1b\][^\x07]*\x07|\r")
# A numbered option line, e.g. " 1. Duo Push to Xperia (Android)".
_OPTION_RE = re.compile(r"^\s*(\d+)[.)]\s+(.*\S)\s*$", re.MULTILINE)


def strip_ansi(text: str) -> str:
    """Remove ANSI escape sequences + carriage returns for clean Slack display."""
    return _ANSI_RE.sub("", text)


def looks_like_prompt(text: str, pattern: str) -> bool:
    """True when *text*'s tail matches *pattern* (an input-waiting prompt)."""
    if not text:
        return False
    try:
        return re.search(pattern, text) is not None
    except re.error:
        # Bad user regex — fall back to "ends with a colon, no trailing newline".
        tail = text.rstrip("\n").splitlines()[-1] if text.strip() else ""
        return tail.rstrip().endswith(":")


def parse_options(text: str) -> list[tuple[str, str]]:
    """Pull ``(number, label)`` choices out of a prompt (e.g. Duo's option list)."""
    return [(m.group(1), m.group(2)) for m in _OPTION_RE.finditer(text)]


# --- host → responder registry (a Slack reply writes to the right tunnel) -----

_responders: dict[str, Callable[[str], Awaitable[bool]]] = {}


def register_responder(host: str, responder: Callable[[str], Awaitable[bool]]) -> None:
    _responders[host] = responder


def unregister_responder(host: str) -> None:
    _responders.pop(host, None)


async def respond(host: str, text: str) -> bool:
    """Deliver *text* (a line, newline appended by the responder) to *host*'s tunnel."""
    responder = _responders.get(host)
    if responder is None:
        return False
    return await responder(text)


def reset_for_testing() -> None:
    _responders.clear()


class PtyProcess:
    """Run a subprocess under a PTY, streaming output and accepting input.

    ``on_output(chunk)`` is awaited for each decoded read. ``write(text)`` sends
    a line (``\\n`` appended) to the process's terminal. The PTY makes the child
    believe it has a real controlling terminal, so ssh emits its interactive auth
    prompts here instead of failing in batch mode.
    """

    def __init__(
        self, cmd: list[str], on_output: Callable[[str], Awaitable[None]]
    ) -> None:
        self._cmd = cmd
        self._on_output = on_output
        self._proc: asyncio.subprocess.Process | None = None
        self._master_fd: int | None = None
        self._loop: asyncio.AbstractEventLoop | None = None

    async def start(self) -> None:
        import pty

        self._loop = asyncio.get_event_loop()
        master_fd, slave_fd = pty.openpty()
        self._master_fd = master_fd
        try:
            self._proc = await asyncio.create_subprocess_exec(
                *self._cmd,
                stdin=slave_fd,
                stdout=slave_fd,
                stderr=slave_fd,
                start_new_session=True,  # ssh's controlling tty = the slave
            )
        finally:
            os.close(slave_fd)  # parent keeps only the master end
        os.set_blocking(master_fd, False)
        self._loop.add_reader(master_fd, self._on_readable)

    def _on_readable(self) -> None:
        assert self._master_fd is not None
        try:
            data = os.read(self._master_fd, 4096)
        except (BlockingIOError, InterruptedError):
            return
        except OSError:
            self._detach_reader()
            return
        if not data:
            self._detach_reader()
            return
        chunk = data.decode("utf-8", errors="replace")
        if self._loop is not None:
            self._loop.create_task(self._on_output(chunk))

    async def write(self, text: str) -> bool:
        if self._master_fd is None:
            return False
        try:
            os.write(self._master_fd, (text + "\n").encode("utf-8"))
            return True
        except OSError:
            return False

    async def wait(self) -> int:
        if self._proc is None:
            return -1
        return await self._proc.wait()

    def _detach_reader(self) -> None:
        if self._loop is not None and self._master_fd is not None:
            with contextlib.suppress(Exception):
                self._loop.remove_reader(self._master_fd)

    async def stop(self) -> None:
        self._detach_reader()
        if self._proc is not None and self._proc.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                self._proc.terminate()
            with contextlib.suppress(asyncio.TimeoutError, Exception):
                await asyncio.wait_for(self._proc.wait(), timeout=5)
        if self._master_fd is not None:
            with contextlib.suppress(OSError):
                os.close(self._master_fd)
            self._master_fd = None


__all__ = [
    "PtyProcess",
    "looks_like_prompt",
    "parse_options",
    "register_responder",
    "reset_for_testing",
    "respond",
    "strip_ansi",
    "unregister_responder",
]
