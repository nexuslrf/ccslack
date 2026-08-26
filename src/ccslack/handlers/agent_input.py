"""Shared path for delivering user text into a bound tmux window.

Both the inbound message handler (``handlers.text``) and the explicit
``/ccslack run`` command (``handlers.meta``) forward a prompt to the agent the
same way — the only difference is how the caller acknowledges it. This module
holds the common delivery: the shell capture/marker bookkeeping plus the tmux
``send-keys``.

``slack_ts`` anchors the shell *marker* path (the eventual exit code lands as a
✅/❌ reaction on that message). ``/ccslack run`` has no message to react to, so
it passes ``None`` and the delivery falls back to the pane-diff capture.
"""

from __future__ import annotations

import structlog

from ..slack_client import BoltSlackClient
from ..tmux_manager import tmux_manager
from ..window_state_store import window_store
from . import shell_capture, shell_marker

logger = structlog.get_logger()

_SendError = (OSError, RuntimeError)

# Agent-native slash commands that switch to a different session transcript.
# When sent from Slack (forwarded to the tmux pane), we flag the window so
# hookless discovery allows the session change instead of blocking it as a
# suspected foreign hijack. /new is excluded (collides with /ccslack new).
_SESSION_SWITCH_COMMANDS = frozenset({"/resume", "/fork", "/clone", "/import"})

# How long the switch flag stays active before auto-clearing (user cancelled
# the picker or the switch failed). Bounds the window's anti-hijack
# vulnerability.
_SESSION_SWITCH_TIMEOUT_SECS = 120.0


def _is_session_switch_command(text: str) -> bool:
    """True when *text* is a native agent command that switches sessions."""
    stripped = text.strip()
    if not stripped:
        return False
    first_word = stripped.split(None, 1)[0].lower()
    return first_word in _SESSION_SWITCH_COMMANDS


async def deliver_to_agent(
    client,  # noqa: ANN001 — Bolt AsyncWebClient
    channel_id: str,
    window_id: str,
    text: str,
    *,
    slack_ts: str | None = None,
) -> bool:
    """Type ``text`` into ``window_id``'s pane, handling shell output capture.

    Returns True if the keystrokes reached tmux, False on send failure.
    """
    is_shell = shell_capture.is_shell_window(window_id)
    use_marker = False

    # Detect a native session-switch command (/resume, /fork, …) forwarded
    # from Slack. Flag the window so hookless discovery allows the next
    # session change instead of blocking it as a foreign hijack.
    if not is_shell and _is_session_switch_command(text):
        window_store.set_session_switch_pending(window_id)

    # Record that a prompt was sent so the auto-toolbar hang detector starts
    # its clock — if the agent produces no output for >2 min, the toolbar
    # auto-opens.
    if not is_shell:
        from .polling.coordinator import mark_prompt_sent

        mark_prompt_sent(window_id)

    if is_shell:
        # Marker path (preferred) only works when we have a Slack message to
        # anchor the exit-code reaction to; otherwise fall back to pane-diff.
        if slack_ts and await shell_marker.has_marker(window_id):
            shell_marker.mark_slack_command(
                window_id, slack_user_message_ts=slack_ts
            )
            use_marker = True
        else:
            await shell_capture.snapshot_pre_send(window_id, command=text)

    try:
        await tmux_manager.send_keys(window_id, text)
    except _SendError:
        logger.exception("send_keys failed for window %s", window_id)
        return False

    if is_shell and not use_marker:
        shell_capture.schedule_capture(BoltSlackClient(client), channel_id, window_id)
    return True


__all__ = ["deliver_to_agent"]
