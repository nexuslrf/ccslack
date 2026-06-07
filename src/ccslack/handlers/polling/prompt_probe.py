"""Detect interactive prompts in providers that don't fire ``Notification`` hooks.

Claude fires a ``Notification`` event whenever its TUI shows a permission /
AskUserQuestion / ExitPlanMode prompt — ``handlers/interactive.py`` consumes
that and posts a Block Kit picker. Codex, Gemini, and shell don't fire that
event, so the same UX requires terminal scraping.

Heuristic: every poll tick, for non-Claude windows whose status is ``idle``,
capture the pane scrollback and look for:

  * **selector marker** — a line starting with ``❯ `` or ``▶ `` (Codex/Gemini
    picker arrow);
  * **numbered list** — two or more lines matching ``\\d+\\.`` (a multi-choice
    prompt);
  * **inline confirm** — ``[y/N]`` / ``(y/n)`` text near the tail.

When at least one signal is present AND the captured pane differs from the
last one we posted for this window, we trigger
``handlers.interactive.handle_pane_prompt`` to render the picker. Each window
keeps the last-posted hash so we don't repost the same prompt every tick.
"""

from __future__ import annotations

import hashlib
import re
import structlog
import time
from typing import TYPE_CHECKING

from ...session import session_manager
from ...tmux_manager import tmux_manager

if TYPE_CHECKING:
    from ...slack_client import SlackClient

logger = structlog.get_logger()

# Per-tick re-extension window for an explicit user dismissal. Each polling
# tick that still finds the dismissed prompt up extends the cooldown by this
# much, so the dismiss effectively lasts as long as the prompt is on-screen.
# Picked to be a couple of polling intervals so a temporary capture failure
# doesn't accidentally end the cooldown early.
DISMISS_COOLDOWN_SECONDS = 30.0

# Providers that fire Notification hooks already — don't double-post.
_HOOK_DRIVEN_PROVIDERS = {"claude", "pi"}

# How much of the pane tail to consider when matching prompts.
_TAIL_LINES = 40
# How many characters to hash for the dedup key.
_HASH_LEN = 32

_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]|\x1b\][^\x07]*\x07")
# Selector glyphs different agents use to mark the active picker line:
#   ❯  fish / heavy right arrow (Claude legacy, ccgram fallback)
#   ▶  filled right triangle (Gemini)
#   ›  single right-pointing angle quote (Codex approval picker)
#   →  rightwards arrow (some custom TUIs)
_SELECTOR_RE = re.compile(r"^[\s│]*[❯▶›→]\s+", re.MULTILINE)
_NUMBERED_RE = re.compile(r"^[\s│]*\d{1,2}[.)]\s+", re.MULTILINE)
_YN_RE = re.compile(r"\[y/n\]|\[Y/n\]|\[y/N\]|\(y/n\)|\(Y/n\)|\(y/N\)", re.IGNORECASE)

# Per-window last-posted prompt hash.
_last_prompt_hash: dict[str, str] = {}
# Per-channel "user explicitly dismissed" cooldown — monotonic seconds.
_dismissed_until: dict[str, float] = {}


def clear_window(window_id: str) -> None:
    """Drop dedup state for a window (called on archive / unbind / activity)."""
    _last_prompt_hash.pop(window_id, None)


def mark_dismissed(channel_id: str) -> None:
    """Block re-posting for this channel until the prompt actually goes away.

    Called from ``handlers/interactive.exit_interactive_mode`` when the user
    explicitly dismisses the picker. ``maybe_post_prompt`` re-extends the
    cooldown on every subsequent tick that still finds the prompt up, so the
    dismiss is effectively permanent until the agent moves on (at which
    point ``_looks_like_prompt`` returns False and the cooldown is cleared
    so a fresh prompt can re-fire).
    """
    _dismissed_until[channel_id] = time.monotonic() + DISMISS_COOLDOWN_SECONDS


def clear_dismiss(channel_id: str) -> None:
    """Drop the dismiss cooldown for this channel (e.g. on archive)."""
    _dismissed_until.pop(channel_id, None)


def _strip_ansi(text: str) -> str:
    return _ANSI_RE.sub("", text)


def _hash_pane(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8"), usedforsecurity=False).hexdigest()[
        :_HASH_LEN
    ]


def _looks_like_prompt(tail: str) -> bool:
    """Return True when the pane tail looks like an interactive selector."""
    selector_hits = len(_SELECTOR_RE.findall(tail))
    numbered_hits = len(_NUMBERED_RE.findall(tail))
    yn = bool(_YN_RE.search(tail))
    # Codex/Gemini: arrow + at least one numbered line.
    # Permission-style: y/n marker anywhere in the tail.
    return (selector_hits >= 1 and numbered_hits >= 1) or yn


async def maybe_post_prompt(
    client: SlackClient,
    channel_id: str,
    window_id: str,
) -> None:
    """Run one probe tick for a window.

    Fallback path — only fires when the JSONL-driven ``enter_interactive_mode``
    hasn't already opened a picker for this channel. Used for shell sessions
    and for providers that show prompts before the matching ``tool_use``
    streams into the transcript.
    """
    # Lazy: interactive module owns the live-picker singleton dict.
    from ..interactive import is_in_interactive_mode

    if is_in_interactive_mode(channel_id):
        # Live picker is open and its own refresh loop is keeping the pane
        # snapshot current — don't post a duplicate.
        _last_prompt_hash.pop(window_id, None)
        return

    view = session_manager.view_window(window_id)
    provider = (view.provider_name if view else "") or ""
    if provider in _HOOK_DRIVEN_PROVIDERS:
        # Claude / Pi handle prompts via Notification → handlers.interactive.
        return

    raw = await tmux_manager.capture_pane_scrollback(
        window_id, history=200, with_ansi=True
    )
    if not raw:
        return
    cleaned = _strip_ansi(raw).rstrip()
    tail_lines = cleaned.splitlines()[-_TAIL_LINES:]
    tail = "\n".join(tail_lines)
    if not _looks_like_prompt(tail):
        # Agent moved on — the prompt the user dismissed is gone. Clear both
        # the dedup hash and the dismiss cooldown so the next prompt
        # (genuinely new content) can fire a fresh picker.
        _last_prompt_hash.pop(window_id, None)
        _dismissed_until.pop(channel_id, None)
        return

    # Honour an explicit dismissal — and *extend* the cooldown for as long
    # as the prompt is still up. A fixed-duration cooldown isn't enough:
    # the agent's prompt typically stays on-screen for minutes (waiting
    # for the user) and pane micro-changes (cursor blink, partial
    # redraws) make the dedup hash differ between ticks. Re-extending
    # the cooldown while the prompt persists makes the dismiss feel
    # permanent until the prompt actually goes away.
    if channel_id in _dismissed_until:
        _dismissed_until[channel_id] = time.monotonic() + DISMISS_COOLDOWN_SECONDS
        # Also refresh the dedup hash so a subsequent post-dismiss tick
        # with the same pane content short-circuits via the cheaper hash
        # check (skip the cooldown branch).
        digest = _hash_pane(tail)
        _last_prompt_hash[window_id] = digest
        return

    digest = _hash_pane(tail)
    if _last_prompt_hash.get(window_id) == digest:
        return
    _last_prompt_hash[window_id] = digest

    # Lazy: interactive module pulls slack_sender + thread_router.
    from ..interactive import enter_from_pane

    await enter_from_pane(
        client,
        channel_id=channel_id,
        window_id=window_id,
        provider=provider or "?",
    )


__all__ = ["clear_window", "maybe_post_prompt"]
