"""Hook-event dispatch from SessionMonitor → Slack channel side effects.

The Claude Code (and Codex / Gemini / Pi) ``ccslack hook`` binary appends each
hook event to ``~/.ccslack/events.jsonl``. SessionMonitor reads that file
incrementally and fires a ``HookEvent`` callback. This module translates those
events into Slack actions:

  * ``Stop`` / ``SessionEnd`` → status flip to "done"
  * ``StopFailure``           → status flip to "done" with error detail
  * ``Notification``          → not yet handled (interactive UI is task-future)
  * ``Subagent*`` / ``Team*`` → not yet handled

The walking-skeleton implementation only wires the ``Stop`` family. Everything
else logs at debug level and is dropped.
"""

from __future__ import annotations

import structlog
from typing import TYPE_CHECKING

from ..config import config
from ..thread_router import thread_router
from ..window_resolver import is_window_id

if TYPE_CHECKING:
    from ..providers.base import HookEvent
    from ..slack_client import SlackClient

logger = structlog.get_logger()


def _strip_window_key(window_key: str) -> str:
    """Hook event window_key is ``"<session>:<window_id>"`` for native windows."""
    if ":" not in window_key:
        return window_key
    prefix, wid = window_key.split(":", 1)
    if prefix == config.tmux_session_name and is_window_id(wid):
        return wid
    return window_key  # Foreign (emdash) — keep as-is.


async def dispatch_hook_event(event: HookEvent, client: SlackClient) -> None:
    """Route a single ``HookEvent`` to the appropriate handler."""
    window_id = _strip_window_key(event.window_key)
    channel_id = thread_router.get_channel_for_window(window_id)
    if channel_id is None:
        logger.debug(
            "Hook event for unbound window: type=%s window=%s",
            event.event_type,
            window_id,
        )
        return

    if event.event_type in ("Stop", "SessionEnd"):
        await _on_stop(client, channel_id, window_id, event)
    elif event.event_type == "StopFailure":
        await _on_stop(client, channel_id, window_id, event, failed=True)
    elif event.event_type == "Notification":
        # Lazy: interactive module pulls tmux capture + Block Kit helpers.
        from .interactive import handle_notification

        await handle_notification(event, client)
    else:
        logger.debug(
            "Unhandled hook event: type=%s window=%s", event.event_type, window_id
        )


async def _on_stop(
    client: SlackClient,
    channel_id: str,
    window_id: str,
    event: HookEvent,
    *,
    failed: bool = False,
) -> None:
    """Flip status to ``done`` (or ``dead`` on failure) on Stop / SessionEnd.

    Also closes any live interactive picker bound to the window — the agent
    finished the turn, so the prompt is no longer outstanding.
    """
    # Lazy: status / interactive modules pull session_manager + slack helpers.
    from .interactive import exit_for_window
    from .messaging_pipeline.turn_threads import end_turn
    from .status import update_status

    await exit_for_window(client, window_id, reason="agent stop")

    # Close the turn's tool-call thread and rewrite its parent into a summary.
    await end_turn(client, channel_id)

    detail: str | None = None
    if failed:
        reason = (event.data or {}).get("reason", "")
        if isinstance(reason, str) and reason:
            detail = f":warning: stop_failure — `{reason[:120]}`"
    await update_status(client, channel_id, window_id, "done", detail=detail)


__all__ = ["dispatch_hook_event"]
