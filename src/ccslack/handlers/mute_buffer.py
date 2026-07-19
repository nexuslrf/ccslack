"""Remember the last agent answer a mute mode suppressed, to flush on un-mute.

A muted / silent channel drops agent output (the monitor advances past it), so
raising the mode back to a chattier one showed nothing until the *next* turn —
which felt like "posting won't resume". This tiny buffer keeps the most recent
suppressed assistant answer per window; ``/ccslack mute`` flushes it when the
channel becomes more verbose, so un-muting immediately surfaces what was missed.

Bounded to one message per window (the latest answer — the thing worth seeing)
and free of heavy imports so both the routing layer and ``handlers.meta`` can
use it without cycles.
"""

from __future__ import annotations

_last_answer: dict[str, str] = {}


def remember(window_id: str, text: str) -> None:
    """Record the latest assistant answer suppressed for ``window_id``."""
    _last_answer[window_id] = text


def take(window_id: str) -> str | None:
    """Pop and return the buffered answer for ``window_id`` (None if empty)."""
    return _last_answer.pop(window_id, None)


def reset() -> None:
    """Clear all buffered answers (test helper)."""
    _last_answer.clear()


__all__ = ["remember", "reset", "take"]
