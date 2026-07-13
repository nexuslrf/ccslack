"""Keep ``/ccslack run`` prompts invisible by dropping their agent-side echo.

``/ccslack run`` is the *quiet* way to prompt the agent — use ``@ccslack`` when
you want the prompt shown in the channel. Its slash invocation is already
ephemeral, but agent providers echo the user's turn back into the channel
(the 🧑 prefix), which would leak the prompt. This tiny registry lets the
routing layer drop exactly one user-echo per ``/run``.

Provider-agnostic and self-cleaning: a suppression that is never matched (shell
and Cursor don't emit a user echo) expires after ``_TTL_SECONDS`` so it can
never swallow a later, legitimate echo. Intentionally free of heavy imports so
both ``handlers.meta`` and the messaging pipeline can import it without cycles.
"""

from __future__ import annotations

import time

_TTL_SECONDS = 30.0
_pending: dict[str, list[float]] = {}


def suppress_next_user_echo(window_id: str) -> None:
    """Register that the next user-echo for ``window_id`` should be dropped."""
    _pending.setdefault(window_id, []).append(time.monotonic() + _TTL_SECONDS)


def consume_user_echo_suppression(window_id: str) -> bool:
    """Return True (and consume one token) if this window's echo should be hidden."""
    deadlines = _pending.get(window_id)
    if not deadlines:
        return False
    now = time.monotonic()
    live = [d for d in deadlines if d > now]
    if not live:
        _pending.pop(window_id, None)
        return False
    live.pop(0)  # consume the oldest still-valid token
    if live:
        _pending[window_id] = live
    else:
        _pending.pop(window_id, None)
    return True


def reset() -> None:
    """Clear all pending suppressions (test helper)."""
    _pending.clear()


__all__ = [
    "consume_user_echo_suppression",
    "reset",
    "suppress_next_user_echo",
]
