"""Session monitoring service — thin coordinator and poll loop.

Orchestrates the session-monitoring subsystem:
  1. Reads hook events via event_reader and dispatches them.
  2. Reconciles session_map changes via SessionLifecycle.
  3. Reads transcript updates via TranscriptReader.
  4. Emits NewMessage / NewWindowEvent to registered callbacks.

All heavy logic lives in the extracted modules:
  - event_reader.py   — reads events.jsonl incrementally
  - idle_tracker.py   — per-session idle timers
  - session_lifecycle.py — session-map diff, claude_task_state authority
  - transcript_reader.py — transcript I/O and parsing

Key classes: SessionMonitor, NewMessage, NewWindowEvent, SessionInfo.
Re-exported from transcript_reader for backward-compatible imports.
"""

import asyncio
import structlog
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from .config import config
from .event_reader import read_new_events
from .idle_tracker import IdleTracker
from .monitor_state import MonitorState
from .providers import get_provider_for_window, registry  # noqa: F401 (used by test patches)
from .session_map import parse_session_map
from .session_lifecycle import session_lifecycle
from .tmux_manager import tmux_manager
from .monitor_events import NewMessage, NewWindowEvent, SessionInfo
from .transcript_reader import TranscriptReader
from .utils import task_done_callback

import aiofiles
import json

# Re-export for backward-compatible imports from other modules
__all__ = [
    "NewMessage",
    "NewWindowEvent",
    "SessionInfo",
    "SessionMonitor",
    "get_active_monitor",
    "set_active_monitor",
]

_CallbackError = Exception
# Chat-transport errors (slack_sdk.errors.SlackApiError) are caught at the
# callback boundary in handlers/messaging_pipeline; the monitor loop only sees
# I/O and parse failures here.
_LoopError = (OSError, RuntimeError, json.JSONDecodeError, ValueError)

_BACKOFF_MIN = 2.0
_BACKOFF_MAX = 30.0
# Rediscovery age cap: find sessions idle up to 24 h, but not ancient ones.
_HOOKLESS_REDISCOVERY_MAX_AGE = 86400.0
# How long the session-switch flag stays active before auto-clearing
# (user cancelled the picker or the switch failed). Bounds the window's
# anti-hijack vulnerability.
_SESSION_SWITCH_TIMEOUT_SECS = 120.0
_MSG_PREVIEW_LENGTH = 80

logger = structlog.get_logger()

_SessionMapError = (json.JSONDecodeError, OSError)


class SessionMonitor:
    """Monitors Claude Code sessions for new assistant messages.

    Thin coordinator: delegates I/O to TranscriptReader, event reading to
    event_reader, session-map diffing to SessionLifecycle, and idle tracking
    to IdleTracker.
    """

    def __init__(
        self,
        projects_path: Path | None = None,
        poll_interval: float | None = None,
        state_file: Path | None = None,
    ):
        self.projects_path = (
            projects_path if projects_path is not None else config.claude_projects_path
        )
        self.poll_interval = (
            poll_interval if poll_interval is not None else config.monitor_poll_interval
        )

        self.state = MonitorState(state_file=state_file or config.monitor_state_file)
        self.state.load()

        self._running = False
        self._task: asyncio.Task | None = None
        self._message_callback: Callable[[NewMessage], Awaitable[None]] | None = None
        self._new_window_callback: (
            Callable[[NewWindowEvent], Awaitable[None]] | None
        ) = None
        # Lazy: providers.base imports HookEvent and gets imported back
        # through tmux_manager → providers; keep at call site.
        # Lazy: HookEvent pulled by hook dispatch path; defer until that path runs
        from .providers.base import HookEvent

        self._hook_event_callback: Callable[[HookEvent], Awaitable[None]] | None = None

        self._idle_tracker = IdleTracker()
        self._transcript_reader = TranscriptReader(self.state, self._idle_tracker)

    # Delegation properties for backward-compatible test access
    @property
    def _last_session_map(self) -> dict:
        return session_lifecycle.last_session_map

    @_last_session_map.setter
    def _last_session_map(self, value: dict) -> None:
        session_lifecycle.initialize(value)

    @property
    def _last_activity(self) -> dict:
        return self._idle_tracker._last_activity

    @property
    def _file_mtimes(self) -> dict:
        return self._transcript_reader._file_mtimes

    @property
    def _pending_tools(self) -> dict:
        return self._transcript_reader._pending_tools

    def get_last_activity(self, session_id: str) -> float | None:
        """Get monotonic timestamp of last transcript activity for a session."""
        return self._idle_tracker.get_last_activity(session_id)

    def set_message_callback(
        self, callback: Callable[[NewMessage], Awaitable[None]]
    ) -> None:
        self._message_callback = callback

    def set_new_window_callback(
        self, callback: Callable[[NewWindowEvent], Awaitable[None]]
    ) -> None:
        self._new_window_callback = callback

    def set_hook_event_callback(self, callback: Callable[..., Awaitable[None]]) -> None:
        self._hook_event_callback = callback

    def record_hook_activity(self, window_id: str) -> None:
        """Record hook-based activity for a window (resets idle timers)."""
        session_id = session_lifecycle.resolve_session_id(window_id)
        if session_id:
            self._idle_tracker.record_activity(session_id)

    async def check_for_updates(self, current_map: dict) -> list[NewMessage]:
        """Check all sessions for new assistant messages.

        Routes sessions to _process_session_file (allowing test spying) and
        delegates the actual I/O to TranscriptReader. Uses _get_active_cwds()
        for fallback session discovery so tests can stub tmux calls.
        """
        new_messages: list[NewMessage] = []
        sid_to_wid = {v["session_id"]: wid for wid, v in current_map.items()}

        direct_sessions: list[tuple[str, Path]] = []
        fallback_session_ids: set[str] = set()

        for details in current_map.values():
            session_id = details["session_id"]
            transcript_path = details.get("transcript_path", "")
            if transcript_path:
                path = Path(transcript_path)
                if path.exists():
                    direct_sessions.append((session_id, path))
                    continue
            fallback_session_ids.add(session_id)

        for session_id, file_path in direct_sessions:
            try:
                await self._process_session_file(
                    session_id,
                    file_path,
                    new_messages,
                    window_id=sid_to_wid.get(session_id, ""),
                )
            except Exception:
                logger.exception("Error processing session %s", session_id)

        if fallback_session_ids:
            active_cwds = await self._get_active_cwds()
            sessions = self._scan_projects_sync(active_cwds) if active_cwds else []
            for session_info in sessions:
                if session_info.session_id not in fallback_session_ids:
                    continue
                try:
                    await self._process_session_file(
                        session_info.session_id,
                        session_info.file_path,
                        new_messages,
                        window_id=sid_to_wid.get(session_info.session_id, ""),
                    )
                except Exception:
                    logger.exception(
                        "Error processing session %s", session_info.session_id
                    )

        self.state.save_if_dirty()
        return new_messages

    async def _process_session_file(
        self, session_id: str, file_path: Path, new_messages: list, window_id: str = ""
    ) -> None:
        """Process a single session file (delegates to TranscriptReader)."""
        await self._transcript_reader._process_session_file(
            session_id, file_path, new_messages, window_id=window_id
        )

    def _scan_projects_sync(self, active_cwds: set) -> list:
        """Scan projects synchronously (delegates to TranscriptReader)."""
        return self._transcript_reader._scan_projects_sync(
            self.projects_path, active_cwds
        )

    async def _get_active_cwds(self) -> set[str]:
        """Get normalized cwds of all active tmux windows (delegates to TranscriptReader)."""
        return await self._transcript_reader._get_active_cwds()

    async def _read_new_lines(
        self, session: Any, file_path: Path, window_id: str = ""
    ) -> list:
        """Read new lines from session file (delegates to TranscriptReader)."""
        return await self._transcript_reader._read_new_lines(
            session, file_path, window_id
        )

    async def _read_hook_events(self) -> None:
        """Read new lines from events.jsonl and dispatch via callback."""
        if not self._hook_event_callback:
            return

        offset_before = self.state.events_offset
        events, new_offset = await read_new_events(
            config.events_file, self.state.events_offset
        )
        self.state.events_offset = new_offset
        if new_offset != offset_before:
            self.state._dirty = True

        for event in events:
            try:
                await self._hook_event_callback(event)
            except _CallbackError:
                logger.exception("Hook event callback error for %s", event.event_type)

    async def _load_current_session_map(self) -> dict[str, dict[str, str]]:
        """Load current session_map and return window_key -> details mapping."""
        if config.session_map_file.exists():
            try:
                async with aiofiles.open(config.session_map_file, "r") as f:
                    content = await f.read()
                raw = json.loads(content)
                prefix = f"{config.tmux_session_name}:"
                return parse_session_map(raw, prefix)
            except _SessionMapError:
                pass
        return {}

    async def _cleanup_all_stale_sessions(self) -> None:
        """Clean up all tracked sessions not in current session_map (startup)."""
        current_map = await self._load_current_session_map()
        active_session_ids = {v["session_id"] for v in current_map.values()}

        stale_sessions = [
            sid for sid in self.state.tracked_sessions if sid not in active_session_ids
        ]
        if stale_sessions:
            logger.info(
                "[Startup cleanup] Removing %d stale sessions", len(stale_sessions)
            )
            for session_id in stale_sessions:
                self._transcript_reader.clear_session(session_id)
                self._idle_tracker.clear_session(session_id)
            self.state.save_if_dirty()

    async def _detect_and_cleanup_changes(self) -> dict[str, dict[str, str]]:
        """Reconcile session_map; clean up replaced/removed sessions; fire new-window events."""
        current_map = await self._load_current_session_map()
        result = session_lifecycle.reconcile(current_map, self._idle_tracker)

        for session_id in result.sessions_to_remove:
            self._transcript_reader.clear_session(session_id)
        if result.sessions_to_remove:
            self.state.save_if_dirty()

        adoption_windows = dict(result.new_windows)
        # Lazy: thread_router is wired into session_manager which imports
        # session_monitor; hoisting forms a startup cycle.
        # Lazy: proxies wired by SessionManager constructor
        from .thread_router import thread_router

        for window_id, details in result.changed_windows.items():
            if not thread_router.has_window(window_id):
                adoption_windows[window_id] = details

        if adoption_windows:
            # Lazy: session.py imports session_monitor at top; hoisting
            # session_manager forms a hard cycle on bootstrap.
            from .session import session_manager as _sm

            for window_id, details in adoption_windows.items():
                provider_name = details.get("provider_name", "")
                if provider_name:
                    _sm.set_window_provider(window_id, provider_name)

                if self._new_window_callback:
                    event = NewWindowEvent(
                        window_id=window_id,
                        session_id=details["session_id"],
                        window_name=details.get("window_name", ""),
                        cwd=details.get("cwd", ""),
                    )
                    try:
                        await self._new_window_callback(event)
                    except _CallbackError:
                        logger.exception("New window callback error for %s", window_id)

        return result.current_map

    async def _discover_hookless_sessions(self, all_windows: list[Any]) -> None:
        """Discover sessions for hookless providers (Codex) and register them.

        Providers with ``supports_hookless_discovery`` write no session_map
        entry via a hook, so their windows never get monitored on their own.
        For each bound window of such a provider, scan the provider's on-disk
        store (``discover_transcript``) on every poll tick. Switch to the
        newest primary session whenever the session_id differs, subject only
        to the already_claimed guard (prevents multiple same-cwd windows from
        racing for the same session). Switching is intentionally immediate so
        that /resume inside the Codex TUI is reflected in Slack within one
        poll tick (~2 s).
        """
        # Lazy: shared-state import cycle (session_map ↔ session_monitor).
        from .session_map import session_map_sync
        from .thread_router import thread_router
        from .window_state_store import window_store

        for window in all_windows:
            window_id = window.window_id
            if not thread_router.has_window(window_id):
                continue  # only windows ccslack bound to a channel
            state = window_store.window_states.get(window_id)
            provider = get_provider_for_window(
                window_id, provider_name=state.provider_name if state else None
            )
            caps = provider.capabilities
            if not caps.supports_hookless_discovery:
                continue
            # When re-discovering (a session is already tracked), raise the
            # provider's default 120 s age cap to 24 h so we can find a
            # session that has been idle but is still the correct one.
            # Unlimited (0) is intentionally avoided: it lets ancient sessions
            # surface and causes multiple same-cwd windows to race.
            rediscovery = bool(state and state.session_id)
            # Session-switch signal: when the user sends /resume (or /fork,
            # /clone, /import) from Slack, agent_input sets a transient flag
            # on the window state. While the flag is active, discovery runs
            # without the anti-hijack guard so it can follow the switch to
            # the new session. The flag clears once a different session is
            # found, or after a timeout (user cancelled the picker).
            switch_from = getattr(state, "session_switch_from", "") if state else ""
            switch_at = getattr(state, "session_switch_at", 0.0) if state else 0.0
            if switch_from:
                import time

                if time.monotonic() - switch_at > _SESSION_SWITCH_TIMEOUT_SECS:
                    # Timed out — user likely cancelled the picker.
                    window_store.clear_session_switch_pending(window_id)
                    switch_from = ""
                else:
                    rediscovery = False  # allow the switch
            event = await asyncio.to_thread(
                provider.discover_transcript,
                window.cwd,
                window_id,
                **{"max_age": _HOOKLESS_REDISCOVERY_MAX_AGE} if rediscovery else {},
            )
            if event is None:
                continue
            if state and state.session_id == event.session_id:
                continue  # already tracking this session — no change
            # In rediscovery mode (session_id already set), never auto-switch.
            # A new session created elsewhere (e.g. remote terminal) should not
            # hijack a bound window. Switching only happens after the user does
            # /resume, which clears session_id → rediscovery becomes False.
            #
            # Exception: when a session-switch flag is active (switch_from is
            # set), rediscovery is already False, so this guard is skipped and
            # the switch proceeds. But if the discovered session is the SAME
            # as the one we're switching from, keep waiting (user is still
            # navigating the picker).
            if rediscovery:
                continue
            if switch_from and event.session_id == switch_from:
                continue  # picker still open — keep tailing the old session
            # Don't claim a session already tracked by another bound window.
            # Multiple windows with the same cwd would otherwise race to own
            # the newest session, causing random cross-channel message leakage.
            already_claimed = any(
                other_ws.session_id == event.session_id
                for other_wid, other_ws in window_store.window_states.items()
                if other_wid != window_id
                and other_ws.session_id
                and thread_router.has_window(other_wid)
            )
            if already_claimed:
                continue
            # Session switch completed — clear the flag.
            if switch_from:
                window_store.clear_session_switch_pending(window_id)
            session_map_sync.register_hookless_session(
                window_id,
                event.session_id,
                event.cwd,
                event.transcript_path,
                caps.name,
            )
            try:
                await asyncio.to_thread(
                    session_map_sync.write_hookless_session_map,
                    window_id,
                    event.session_id,
                    event.cwd,
                    event.transcript_path,
                    caps.name,
                )
            except OSError:
                logger.exception(
                    "Failed to write hookless session_map for %s", window_id
                )
                continue
            logger.info(
                "Discovered %s session %s for window %s",
                caps.name,
                event.session_id,
                window_id,
            )

    async def _monitor_loop(self) -> None:
        """Background poll loop."""
        logger.info("Session monitor started, polling every %ss", self.poll_interval)

        # Lazy: session_map imports session_monitor types via shared
        # state cycle; keep at call site.
        # Lazy: proxies wired by SessionManager constructor
        from .session_map import session_map_sync

        await self._cleanup_all_stale_sessions()
        initial_map = await self._load_current_session_map()
        session_lifecycle.initialize(initial_map)

        error_streak = 0
        while self._running:
            try:
                await self._read_hook_events()
                await session_map_sync.load_session_map()

                current_map = await self._detect_and_cleanup_changes()

                all_windows = await tmux_manager.list_windows()
                external_windows = await tmux_manager.discover_external_sessions()
                all_windows = all_windows + external_windows
                live_window_ids = {w.window_id for w in all_windows}
                session_map_sync.prune_session_map(live_window_ids)
                known_window_ids = set(current_map.keys())
                await self._discover_hookless_sessions(all_windows)
                for window in all_windows:
                    if window.window_id in known_window_ids:
                        continue
                    # Lazy: same cycle as the earlier thread_router import.
                    from .thread_router import thread_router

                    already_bound = thread_router.has_window(window.window_id)
                    if not already_bound and self._new_window_callback:
                        event = NewWindowEvent(
                            window_id=window.window_id,
                            session_id="",
                            window_name=window.window_name,
                            cwd=window.cwd,
                        )
                        try:
                            await self._new_window_callback(event)
                        except _CallbackError:
                            logger.exception(
                                "New window callback error for %s",
                                window.window_id,
                            )

                new_messages = await self.check_for_updates(current_map)

                for msg in new_messages:
                    structlog.contextvars.clear_contextvars()
                    structlog.contextvars.bind_contextvars(session_id=msg.session_id)
                    status = "complete" if msg.is_complete else "streaming"
                    preview = msg.text[:_MSG_PREVIEW_LENGTH] + (
                        "..." if len(msg.text) > _MSG_PREVIEW_LENGTH else ""
                    )
                    logger.debug("[%s] session=%s: %s", status, msg.session_id, preview)
                    if self._message_callback:
                        try:
                            await self._message_callback(msg)
                        except _CallbackError:
                            logger.exception(
                                "Message callback error for session=%s",
                                msg.session_id,
                            )

            except _LoopError:
                logger.exception("Monitor loop error")
                backoff_delay = min(_BACKOFF_MAX, _BACKOFF_MIN * (2**error_streak))
                error_streak += 1
                await asyncio.sleep(backoff_delay)
                continue
            except Exception:
                logger.exception("Unexpected error in monitor loop")
                backoff_delay = min(_BACKOFF_MAX, _BACKOFF_MIN * (2**error_streak))
                error_streak += 1
                await asyncio.sleep(backoff_delay)
                continue

            error_streak = 0
            await asyncio.sleep(self.poll_interval)

        logger.info("Session monitor stopped")

    def start(self) -> None:
        if self._running:
            logger.debug("Monitor already running")
            return
        self._running = True
        self._task = asyncio.create_task(self._monitor_loop())
        self._task.add_done_callback(task_done_callback)

    def stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()
            self._task = None
        self.state.save()
        logger.info("Session monitor stopped and state saved")


_active_monitor: SessionMonitor | None = None


def set_active_monitor(monitor: SessionMonitor) -> None:
    """Set the active SessionMonitor instance (called by bot.py post_init)."""
    global _active_monitor  # noqa: PLW0603
    _active_monitor = monitor


def clear_active_monitor() -> None:
    """Clear the active SessionMonitor singleton (shutdown / test reset)."""
    global _active_monitor  # noqa: PLW0603
    _active_monitor = None


def get_active_monitor() -> SessionMonitor | None:
    """Return the active SessionMonitor instance."""
    return _active_monitor
