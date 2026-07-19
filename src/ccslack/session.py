"""Slack session management — the core state hub.

Manages the key mappings:
  Window→Session (window_states): which Claude session_id a window holds
    (keyed by window_id).
  Channel→Window: delegated to ThreadRouter (see thread_router.py).

Responsibilities:
  - Persist/load state to ~/.ccslack/state.json.
  - Sync window↔session bindings from session_map.json (written by hook).
  - Delegate channel↔window routing to ThreadRouter.
  - Re-resolve stale window IDs on startup (tmux server restart recovery).

Key class: SessionManager (singleton instantiated as ``session_manager``).
"""

import json
import structlog
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .config import config
from .session_map import (
    SessionMapSync,
    install_session_map_sync,
    session_map_sync,
)
from .state_persistence import StatePersistence
from .tmux_manager import tmux_manager
from .thread_router import ThreadRouter, install_thread_router, thread_router
from .user_preferences import (
    UserPreferences,
    install_user_preferences,
    user_preferences,
)
from .window_resolver import EMDASH_SESSION_PREFIX, is_foreign_window, is_window_id
from .window_view import WindowView
from .window_state_store import (
    APPROVAL_MODES,
    BATCH_MODES,
    DEFAULT_APPROVAL_MODE,
    DEFAULT_BATCH_MODE,
    NOTIFICATION_MODES,
    WindowState,
    WindowStateStore,
    install_window_store,
    window_store,
)

logger = structlog.get_logger()


@dataclass
class AuditIssue:
    """A single issue found during state audit."""

    category: str
    detail: str
    fixable: bool


@dataclass
class AuditResult:
    """Result of a state audit."""

    issues: list[AuditIssue]
    total_bindings: int
    live_binding_count: int

    @property
    def fixable_count(self) -> int:
        return sum(1 for i in self.issues if i.fixable)

    @property
    def has_issues(self) -> bool:
        return len(self.issues) > 0


@dataclass
class SessionManager:
    """Manages session state for ccslack.

    All internal keys use window_id (e.g. '@0', '@12') for uniqueness.
    Display names (window_name) are stored separately for UI presentation.

    Channel routing (channel_bindings, display names) is delegated to
    ThreadRouter — see thread_router.py.

    window_states: window_id -> WindowState (session_id, cwd, window_name)

    User preferences (starred dirs, MRU, read offsets) are delegated to
    UserPreferences — see user_preferences.py.
    """

    _persistence: StatePersistence = field(default=None, repr=False, init=False)  # type: ignore[assignment]

    @property
    def window_states(self) -> dict[str, WindowState]:
        return window_store.window_states

    @property
    def channel_bindings(self) -> dict[str, str]:
        return thread_router.channel_bindings

    @property
    def window_display_names(self) -> dict[str, str]:
        return thread_router.window_display_names

    def __post_init__(self) -> None:
        self._persistence = StatePersistence(config.state_file, self._serialize_state)
        self._window_store = WindowStateStore(
            schedule_save=self._save_state,
            on_hookless_provider_switch=self._clear_session_map_entry,
        )
        install_window_store(self._window_store)
        self._thread_router = ThreadRouter(
            schedule_save=self._save_state,
            has_window_state=self._window_store.has_window,
        )
        install_thread_router(self._thread_router)
        self._user_preferences = UserPreferences(schedule_save=self._save_state)
        install_user_preferences(self._user_preferences)
        self._session_map_sync = SessionMapSync(schedule_save=self._save_state)
        install_session_map_sync(self._session_map_sync)
        self._load_state()

    def _serialize_state(self) -> dict[str, Any]:
        """Serialize all state to a dict for persistence."""
        result: dict[str, Any] = {"window_states": window_store.to_dict()}
        result.update(user_preferences.to_dict())
        result.update(thread_router.to_dict())
        return result

    def _save_state(self) -> None:
        """Schedule debounced save (0.5s delay, resets on each call)."""
        self._persistence.schedule_save()

    def flush_state(self) -> None:
        """Force immediate save. Call on shutdown."""
        self._persistence.flush()

    def _is_window_id(self, key: str) -> bool:
        """Check if a key looks like a tmux window ID (e.g. '@0', '@12')."""
        return is_window_id(key)

    def _load_state(self) -> None:
        """Load state during initialization."""
        state = self._persistence.load()
        if not state:
            return

        window_store.from_dict(state.get("window_states", {}))
        user_preferences.from_dict(state)
        thread_router.from_dict(state)

        needs_migration = False
        for k in window_store.window_states:
            if not self._is_window_id(k) and not is_foreign_window(k):
                needs_migration = True
                break
        if not needs_migration:
            for wid in thread_router.channel_bindings.values():
                if not self._is_window_id(wid) and not is_foreign_window(wid):
                    needs_migration = True
                    break

        if needs_migration:
            logger.info(
                "Detected old-format state (window_name keys), "
                "will re-resolve on startup"
            )

    async def resolve_stale_ids(self) -> None:
        """Re-resolve persisted window IDs against live tmux windows."""
        # Lazy: window_resolver pulls back into session manager
        from .window_resolver import LiveWindow, resolve_stale_ids as _resolve

        windows = await tmux_manager.list_windows()
        live = [
            LiveWindow(window_id=w.window_id, window_name=w.window_name)
            for w in windows
        ]

        # Adapt user_preferences.user_window_offsets into a single per-key dict
        # for the resolver helper. ccslack's offset shape is still per-user;
        # we feed all users' offsets at once.
        # The resolver mutates the inner dicts in place.
        changed = _resolve(
            live,
            self.window_states,
            thread_router.channel_bindings,
            user_preferences.user_window_offsets,
            thread_router.window_display_names,
        )

        if changed:
            thread_router._rebuild_reverse_index()
            self._save_state()
            logger.info("Startup re-resolution complete")

        live_ids = {w.window_id for w in live}
        session_map_sync.prune_session_map(live_ids)

        live_pairs = [(w.window_id, w.window_name) for w in live]
        self.sync_display_names(live_pairs)

        self.prune_stale_state(live_ids)

    # --- Display name management ---

    def set_display_name(self, window_id: str, window_name: str) -> None:
        """Update display name for a window_id."""
        thread_router.set_display_name(window_id, window_name)
        ws = self.window_states.get(window_id)
        if ws:
            ws.window_name = window_name

    def sync_display_names(self, live_windows: list[tuple[str, str]]) -> bool:
        """Sync display names from live tmux windows. Returns True if changed."""
        router_changed = thread_router.sync_display_names(live_windows)
        ws_changed = False
        for window_id, window_name in live_windows:
            ws = self.window_states.get(window_id)
            if ws and ws.window_name != window_name:
                ws.window_name = window_name
                ws_changed = True
        if ws_changed and not router_changed:
            self._save_state()
        return router_changed or ws_changed

    def prune_stale_state(self, live_window_ids: set[str]) -> bool:
        """Remove orphaned entries from window_display_names. Returns True if changed."""
        in_use = set(self.window_states.keys())
        in_use.update(thread_router.channel_bindings.values())

        stale_display = [
            wid
            for wid in thread_router.window_display_names
            if wid not in live_window_ids and wid not in in_use
        ]

        all_known = live_window_ids | in_use
        offsets_changed = user_preferences.prune_stale_offsets(all_known)

        if not stale_display:
            return offsets_changed

        for wid in stale_display:
            name = thread_router.pop_display_name(wid)
            logger.info("Pruning stale display name: %s (%s)", wid, name)

        self._save_state()
        return True

    def _get_session_map_window_ids(self) -> set[str]:
        """Read session_map.json and return window IDs tracked by ccslack."""
        if not config.session_map_file.exists():
            return set()
        try:
            raw = json.loads(config.session_map_file.read_text())
        except json.JSONDecodeError, OSError:
            return set()
        prefix = f"{config.tmux_session_name}:"
        result: set[str] = set()
        for key in raw:
            if key.startswith(prefix):
                wid = key[len(prefix) :]
                if self._is_window_id(wid):
                    result.add(wid)
            elif key.startswith(EMDASH_SESSION_PREFIX):
                result.add(key)
        return result

    def audit_state(
        self,
        live_window_ids: set[str],
        live_windows: list[tuple[str, str]],
    ) -> AuditResult:
        """Read-only audit of all state maps against live tmux windows."""
        issues: list[AuditIssue] = []

        bound_window_ids: set[str] = set()
        total_bindings = 0
        live_binding_count = 0
        for _channel_id, wid in thread_router.channel_bindings.items():
            total_bindings += 1
            bound_window_ids.add(wid)
            if wid in live_window_ids:
                live_binding_count += 1

        session_map_wids = self._get_session_map_window_ids()

        # 1. Ghost bindings (channel → dead window) — fixable (archive channel).
        for channel_id, wid in thread_router.channel_bindings.items():
            if wid not in live_window_ids:
                display = thread_router.get_display_name(wid)
                issues.append(
                    AuditIssue(
                        category="ghost_binding",
                        detail=f"channel:{channel_id} window:{wid} ({display})",
                        fixable=True,
                    )
                )

        # 2. Orphaned display names.
        in_use = set(self.window_states.keys()) | bound_window_ids
        for wid in thread_router.window_display_names:
            if wid not in live_window_ids and wid not in in_use:
                name = thread_router.get_display_name(wid)
                issues.append(
                    AuditIssue(
                        category="orphaned_display_name",
                        detail=f"{wid} ({name})",
                        fixable=True,
                    )
                )

        # 3. Stale window_states (not in session_map, not bound, not live).
        for wid in self.window_states:
            if (
                wid not in session_map_wids
                and wid not in bound_window_ids
                and wid not in live_window_ids
            ):
                display = self.window_states[wid].window_name or wid
                issues.append(
                    AuditIssue(
                        category="stale_window_state",
                        detail=f"{wid} ({display})",
                        fixable=True,
                    )
                )

        # 4. Display name drift (stored != tmux).
        for wid, tmux_name in live_windows:
            stored_name = thread_router.window_display_names.get(wid)
            if stored_name and stored_name != tmux_name:
                issues.append(
                    AuditIssue(
                        category="display_name_drift",
                        detail=f"{wid}: stored={stored_name!r} tmux={tmux_name!r}",
                        fixable=True,
                    )
                )

        # 5. Orphaned tmux windows (live, known, but unbound to any channel).
        known_wids = session_map_wids | set(self.window_states.keys())
        for wid in live_window_ids:
            if wid not in bound_window_ids and wid in known_wids:
                name = dict(live_windows).get(wid, wid)
                issues.append(
                    AuditIssue(
                        category="orphaned_window",
                        detail=f"{wid} ({name})",
                        fixable=True,
                    )
                )

        return AuditResult(
            issues=issues,
            total_bindings=total_bindings,
            live_binding_count=live_binding_count,
        )

    def prune_stale_window_states(self, live_window_ids: set[str]) -> bool:
        """Remove window_states not in session_map, not bound, and not live."""
        session_map_wids = self._get_session_map_window_ids()
        bound_window_ids: set[str] = set(thread_router.channel_bindings.values())

        stale = [
            wid
            for wid in self.window_states
            if (
                wid not in session_map_wids
                and wid not in bound_window_ids
                and wid not in live_window_ids
            )
        ]
        if not stale:
            return False
        for wid in stale:
            logger.info("Pruning stale window_state: %s", wid)
            del self.window_states[wid]
        self._save_state()
        return True

    # --- Window state management ---

    def view_window(self, window_id: str) -> WindowView | None:
        """Read-only snapshot of a window's state."""
        ws = window_store.window_states.get(window_id)
        if ws is None:
            return None
        return WindowView(
            window_id=window_id,
            cwd=ws.cwd or "",
            provider_name=ws.provider_name,
            approval_mode=ws.approval_mode,
            notification_mode=ws.notification_mode,
            batch_mode=ws.batch_mode,
            tool_call_visibility=ws.tool_call_visibility,
            transcript_path=Path(ws.transcript_path) if ws.transcript_path else None,
            window_name=ws.window_name,
            session_id=ws.session_id,
            external=ws.external,
            origin=ws.origin,
        )

    @property
    def window_count(self) -> int:
        return len(window_store.window_states)

    def iter_window_ids(self) -> list[str]:
        return list(window_store.window_states.keys())

    # --- Provider management ---

    def set_window_provider(
        self,
        window_id: str,
        provider_name: str,
        *,
        cwd: str | None = None,
    ) -> None:
        """Set the provider for a window."""
        supports_hook = True
        if provider_name:
            # Lazy: providers.registry imports concrete provider modules
            # which transitively touch session state; keep lookup local.
            from .providers.registry import UnknownProviderError, registry

            try:
                supports_hook = registry.get(provider_name).capabilities.supports_hook
            except UnknownProviderError:
                supports_hook = True
        window_store.set_window_provider(
            window_id,
            provider_name,
            cwd=cwd,
            new_provider_supports_hook=supports_hook,
        )

    def _clear_session_map_entry(self, window_id: str) -> None:
        session_map_sync.clear_session_map_entry(window_id)

    def set_window_cwd(self, window_id: str, cwd: str) -> None:
        state = window_store.get_window_state(window_id)
        state.cwd = cwd
        self._save_state()

    def set_window_origin(self, window_id: str, origin: str) -> None:
        window_store.set_window_origin(window_id, origin)

    def set_window_worktree(
        self, window_id: str, worktree_path: str, branch: str
    ) -> None:
        state = window_store.get_window_state(window_id)
        state.worktree_path = worktree_path
        state.worktree_branch = branch
        self._save_state()

    # --- Approval / notification / batch / tool-call cycling (provider-agnostic) ---

    def get_approval_mode(self, window_id: str) -> str:
        state = self.window_states.get(window_id)
        mode = state.approval_mode if state else DEFAULT_APPROVAL_MODE
        return mode if mode in APPROVAL_MODES else DEFAULT_APPROVAL_MODE

    def set_window_approval_mode(self, window_id: str, mode: str) -> None:
        normalized = mode.lower()
        if normalized not in APPROVAL_MODES:
            raise ValueError(f"Invalid approval mode: {mode!r}")
        state = window_store.get_window_state(window_id)
        state.approval_mode = normalized
        self._save_state()

    _NOTIFICATION_MODES = NOTIFICATION_MODES

    def get_notification_mode(self, window_id: str) -> str:
        state = self.window_states.get(window_id)
        return state.notification_mode if state else "all"

    def set_notification_mode(self, window_id: str, mode: str) -> None:
        if mode not in self._NOTIFICATION_MODES:
            raise ValueError(f"Invalid notification mode: {mode!r}")
        state = window_store.get_window_state(window_id)
        if state.notification_mode != mode:
            state.notification_mode = mode
            self._save_state()

    def cycle_notification_mode(self, window_id: str) -> str:
        current = self.get_notification_mode(window_id)
        modes = self._NOTIFICATION_MODES
        idx = modes.index(current) if current in modes else 0
        new_mode = modes[(idx + 1) % len(modes)]
        self.set_notification_mode(window_id, new_mode)
        return new_mode

    def get_input_mode(self, window_id: str) -> str:
        return window_store.get_input_mode(window_id)

    def set_input_mode(self, window_id: str, mode: str) -> None:
        window_store.set_input_mode(window_id, mode)

    def toggle_input_mode(self, window_id: str) -> str:
        return window_store.toggle_input_mode(window_id)

    def get_commentary_visibility(self, window_id: str) -> str:
        return window_store.get_commentary_visibility(window_id)

    def set_commentary_visibility(self, window_id: str, mode: str) -> None:
        window_store.set_commentary_visibility(window_id, mode)

    def toggle_commentary_visibility(self, window_id: str) -> str:
        return window_store.toggle_commentary_visibility(window_id)

    def get_batch_mode(self, window_id: str) -> str:
        state = self.window_states.get(window_id)
        mode = state.batch_mode if state else DEFAULT_BATCH_MODE
        return mode if mode in BATCH_MODES else DEFAULT_BATCH_MODE

    def set_batch_mode(self, window_id: str, mode: str) -> None:
        if mode not in BATCH_MODES:
            raise ValueError(f"Invalid batch mode: {mode!r}")
        state = window_store.get_window_state(window_id)
        if state.batch_mode != mode:
            state.batch_mode = mode
            self._save_state()

    def cycle_batch_mode(self, window_id: str) -> str:
        current = self.get_batch_mode(window_id)
        new_mode = "verbose" if current == "batched" else "batched"
        self.set_batch_mode(window_id, new_mode)
        return new_mode

    def get_tool_call_visibility(self, window_id: str) -> str:
        return window_store.get_tool_call_visibility(window_id)

    def set_tool_call_visibility(self, window_id: str, mode: str) -> None:
        window_store.set_tool_call_visibility(window_id, mode)

    def cycle_tool_call_visibility(self, window_id: str) -> str:
        return window_store.cycle_tool_call_visibility(window_id)

    def get_thread_tool_calls(self, window_id: str) -> str:
        return window_store.get_thread_tool_calls(window_id)

    def set_thread_tool_calls(self, window_id: str, mode: str) -> None:
        window_store.set_thread_tool_calls(window_id, mode)

    def cycle_thread_tool_calls(self, window_id: str) -> str:
        return window_store.cycle_thread_tool_calls(window_id)


session_manager = SessionManager()
