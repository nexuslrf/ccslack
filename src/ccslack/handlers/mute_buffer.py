"""Remember the last one or two agent answers a mute mode suppressed, to flush on un-mute.

A muted / silent channel drops agent output (the monitor advances past it), so
raising the mode back to a chattier one showed nothing until the *next* turn —
which felt like "posting won't resume". This tiny buffer keeps the most recent
suppressed assistant answers per window; ``/ccslack mute`` flushes them when the
channel becomes more verbose, so un-muting immediately surfaces what was missed.

Bounded to the ``_MAX_BUFFERED`` most recent answers per window: un-muting a
long silent stretch shouldn't dump the whole backlog — one or two final answers
is enough to re-orient, and the rest was already consumed by the monitor. Kept
free of heavy imports so both the routing layer and ``handlers.meta`` can use it
without cycles.
"""

from __future__ import annotations

# Keep at most this many recent suppressed answers per window.
_MAX_BUFFERED = 2

_recent_answers: dict[str, list[str]] = {}


def remember(window_id: str, text: str) -> None:
    """Record a suppressed assistant answer, keeping the last ``_MAX_BUFFERED``."""
    answers = _recent_answers.setdefault(window_id, [])
    answers.append(text)
    # Trim in place to the most recent _MAX_BUFFERED (no-op while under the cap).
    del answers[:-_MAX_BUFFERED]


def take(window_id: str) -> str | None:
    """Pop the buffered answers for ``window_id``, oldest-first (None if empty)."""
    answers = _recent_answers.pop(window_id, None)
    if not answers:
        return None
    return "\n\n".join(answers)


def reset() -> None:
    """Clear all buffered answers (test helper)."""
    _recent_answers.clear()


__all__ = ["remember", "reset", "take"]
