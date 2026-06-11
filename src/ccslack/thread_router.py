"""Channel routing — Slack channel ↔ tmux window binding.

Maps Slack channels (channel_id) to tmux windows (window_id) bidirectionally.
Each session lives in its own private Slack channel; the channel ID is the
unique routing key (workspace-unique by Slack, no per-user dimension).

Naming note: the class is still called ``ThreadRouter`` to preserve symmetry
with the ccgram source modules ported into ``session_map.py`` /
``session_monitor.py`` / ``session_resolver.py``. In ccslack the "thread" is
in fact a channel; thread-naming inherited from ccgram for parity.

Key class: ThreadRouter. Persistence and window-state queries are injected
via the constructor — the router cannot be built without explicit callbacks.

Module-level access: ``get_thread_router()`` returns the SessionManager-owned
instance; the legacy module attribute ``thread_router`` is a thin proxy that
delegates to the same instance.

Key data:
  - channel_bindings    (channel_id → window_id)
  - _window_to_channel  (reverse index for O(1) inbound lookups)
  - window_display_names (window_id → display name)
"""

from __future__ import annotations

import structlog
from collections.abc import Callable, Iterator
from typing import Any, cast

logger = structlog.get_logger()


class ThreadRouter:
    """Bidirectional mapping between Slack channels and tmux windows.

    Persistence and window-state queries are injected via the constructor:

    * ``schedule_save``: triggers a debounced save after mutations.
    * ``has_window_state``: returns True when a window has tracked WindowState —
      used to decide whether a display name is still load-bearing during
      ``unbind_channel``.
    """

    def __init__(
        self,
        *,
        schedule_save: Callable[[], None],
        has_window_state: Callable[[str], bool],
    ) -> None:
        # channel_id → window_id (1:1 — enforced on bind).
        self.channel_bindings: dict[str, str] = {}
        # window_id → display name.
        self.window_display_names: dict[str, str] = {}
        # window_id → channel_id (reverse for O(1) inbound lookups).
        self._window_to_channel: dict[str, str] = {}
        # channel_id → set of parent ts marked as human-only "chat" threads.
        # Replies under these threads are NOT forwarded to tmux.
        self.chat_threads: dict[str, set[str]] = {}
        self._schedule_save: Callable[[], None] = schedule_save
        self._has_window_state: Callable[[str], bool] = has_window_state

    def reset(self) -> None:
        """Clear all state. Used for test isolation."""
        self.channel_bindings.clear()
        self.window_display_names.clear()
        self._window_to_channel.clear()
        self.chat_threads.clear()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _rebuild_reverse_index(self) -> None:
        """Rebuild _window_to_channel from channel_bindings."""
        self._window_to_channel = {
            window_id: channel_id
            for channel_id, window_id in self.channel_bindings.items()
        }

    def _dedup_channel_bindings(self) -> None:
        """Enforce 1 window = 1 channel. Keep first channel encountered."""
        window_channels: dict[str, list[str]] = {}
        for channel_id, window_id in self.channel_bindings.items():
            window_channels.setdefault(window_id, []).append(channel_id)
        for window_id, channels in window_channels.items():
            if len(channels) > 1:
                keep = channels[0]
                for channel_id in channels[1:]:
                    del self.channel_bindings[channel_id]
                    logger.warning(
                        "Startup: removed duplicate binding channel %s -> window %s "
                        "(keeping channel %s)",
                        channel_id,
                        window_id,
                        keep,
                    )

    # ------------------------------------------------------------------
    # Serialization
    # ------------------------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        """Serialize routing state for state.json persistence."""
        return {
            "channel_bindings": dict(self.channel_bindings),
            "window_display_names": dict(self.window_display_names),
            "chat_threads": {
                channel_id: sorted(ts_set)
                for channel_id, ts_set in self.chat_threads.items()
                if ts_set
            },
        }

    def from_dict(self, data: dict[str, Any]) -> None:
        """Restore routing state from persisted data.

        Does NOT call ``_schedule_save`` — loading from disk must not trigger
        a write.
        """
        # Accept both new "channel_bindings" key and legacy "thread_bindings"
        # shape so a state.json migrated from ccgram (user→thread→wid) can be
        # at least partially recovered if anyone hand-mapped it. Anything else
        # is ignored silently.
        raw_channels = data.get("channel_bindings", {})
        if isinstance(raw_channels, dict):
            self.channel_bindings = {
                str(channel_id): str(window_id)
                for channel_id, window_id in raw_channels.items()
                if isinstance(window_id, str) and window_id
            }
        else:
            self.channel_bindings = {}
        self.window_display_names = dict(data.get("window_display_names", {}))
        raw_chat = data.get("chat_threads", {})
        if isinstance(raw_chat, dict):
            self.chat_threads = {
                str(channel_id): {str(ts) for ts in ts_list}
                for channel_id, ts_list in raw_chat.items()
                if isinstance(ts_list, list) and ts_list
            }
        else:
            self.chat_threads = {}
        self._dedup_channel_bindings()
        self._rebuild_reverse_index()

    # ------------------------------------------------------------------
    # Channel binding operations
    # ------------------------------------------------------------------

    def bind_channel(
        self, channel_id: str, window_id: str, window_name: str = ""
    ) -> None:
        """Bind a Slack channel to a tmux window.

        Enforces 1 channel = 1 window: if another channel is already bound to
        the same window_id, that stale binding is removed first.
        """
        stale_channels = [
            existing_channel
            for existing_channel, existing_window in self.channel_bindings.items()
            if existing_window == window_id and existing_channel != channel_id
        ]
        for stale in stale_channels:
            del self.channel_bindings[stale]
            logger.info(
                "Evicted stale binding: channel %s -> window_id %s "
                "(replaced by channel %s)",
                stale,
                window_id,
                channel_id,
            )

        old_window = self.channel_bindings.get(channel_id)
        if old_window is not None and old_window != window_id:
            self._window_to_channel.pop(old_window, None)

        self.channel_bindings[channel_id] = window_id
        self._window_to_channel[window_id] = channel_id
        if window_name:
            self.window_display_names[window_id] = window_name
        self._schedule_save()
        display = window_name or self.get_display_name(window_id)
        logger.info(
            "Bound channel %s -> window_id %s (%s)",
            channel_id,
            window_id,
            display,
        )

    def unbind_channel(self, channel_id: str) -> str | None:
        """Remove a channel binding. Returns the previously bound window_id.

        Cleans up the reverse index. Display name lifecycle is handled here:
        if no other channel references this window and no WindowState tracks
        it, the display name is removed.
        """
        window_id = self.channel_bindings.pop(channel_id, None)
        if window_id is None:
            return None
        self._window_to_channel.pop(window_id, None)
        logger.info("Unbound channel %s (was %s)", channel_id, window_id)

        still_bound = any(wid == window_id for wid in self.channel_bindings.values())
        if not still_bound and not self._has_window_state(window_id):
            self.window_display_names.pop(window_id, None)

        # Chat-thread markers are channel-scoped; drop them with the binding.
        self.chat_threads.pop(channel_id, None)

        self._schedule_save()
        return window_id

    def get_window_for_channel(self, channel_id: str) -> str | None:
        """Look up the window_id bound to a channel."""
        return self.channel_bindings.get(channel_id)

    def effective_window_id(self, channel_id: str, fallback: str = "") -> str:
        """Resolve a channel's current window, preferring the live binding.

        Action buttons embed the window_id that was current when the message
        was posted, but a restore can rebind the channel to a NEW window_id
        (the old window died and a fresh one was spawned). Since
        1 channel = 1 window, the binding is the source of truth — prefer it,
        and only fall back to the embedded button value when the channel is no
        longer bound.
        """
        return self.channel_bindings.get(channel_id) or fallback

    # ------------------------------------------------------------------
    # Chat threads (human-only side conversations)
    # ------------------------------------------------------------------

    def mark_chat_thread(self, channel_id: str, thread_ts: str) -> None:
        """Mark a thread (by parent ts) as human-only — replies skip tmux."""
        if not channel_id or not thread_ts:
            return
        self.chat_threads.setdefault(channel_id, set()).add(thread_ts)
        self._schedule_save()

    def is_chat_thread(self, channel_id: str, thread_ts: str) -> bool:
        """True if *thread_ts* in *channel_id* is a human-only chat thread."""
        return thread_ts in self.chat_threads.get(channel_id, set())

    def get_channel_for_window(self, window_id: str) -> str | None:
        """Reverse lookup: get channel_id for a window (O(1))."""
        return self._window_to_channel.get(window_id)

    def has_window(self, window_id: str) -> bool:
        """Check if any channel has a binding to this window_id."""
        return window_id in self._window_to_channel

    def has_channel(self, channel_id: str) -> bool:
        """Check if this channel is bound to any window."""
        return channel_id in self.channel_bindings

    def iter_channel_bindings(self) -> Iterator[tuple[str, str]]:
        """Iterate all bindings as (channel_id, window_id)."""
        yield from self.channel_bindings.items()

    # ------------------------------------------------------------------
    # Display name management
    # ------------------------------------------------------------------

    def get_display_name(self, window_id: str) -> str:
        """Get display name for a window_id, fallback to window_id itself."""
        return self.window_display_names.get(window_id, window_id)

    def pop_display_name(self, window_id: str) -> str:
        """Remove and return display name for window_id. Falls back to window_id."""
        if window_id not in self.window_display_names:
            return window_id
        name = self.window_display_names.pop(window_id)
        self._schedule_save()
        return name

    def set_display_name(self, window_id: str, window_name: str) -> None:
        """Update display name for a window_id."""
        if self.window_display_names.get(window_id) != window_name:
            self.window_display_names[window_id] = window_name
            self._schedule_save()

    def sync_display_names(self, live_windows: list[tuple[str, str]]) -> bool:
        """Sync display names from live tmux windows. Returns True if changed."""
        changed = False
        for window_id, window_name in live_windows:
            old = self.window_display_names.get(window_id)
            if old and old != window_name:
                self.window_display_names[window_id] = window_name
                changed = True
                logger.info(
                    "Synced display name: %s %s → %s", window_id, old, window_name
                )
        if changed:
            self._schedule_save()
        return changed


_active_router: ThreadRouter | None = None


def get_thread_router() -> ThreadRouter:
    """Return the SessionManager-owned ThreadRouter."""
    if _active_router is None:
        raise RuntimeError(
            "ThreadRouter not yet wired. "
            "Instantiate SessionManager() before accessing thread_router."
        )
    return _active_router


def install_thread_router(router: ThreadRouter) -> None:
    """Install the SessionManager-owned router as the module-level singleton."""
    global _active_router
    _active_router = router


class _ThreadRouterProxy:
    """Module-level facade that resolves to the wired router."""

    __slots__ = ()

    def __getattr__(self, name: str) -> Any:
        return getattr(get_thread_router(), name)

    def __setattr__(self, name: str, value: Any) -> None:
        setattr(get_thread_router(), name, value)

    def __delattr__(self, name: str) -> None:
        delattr(get_thread_router(), name)

    def __repr__(self) -> str:
        if _active_router is None:
            return "<ThreadRouterProxy unwired>"
        return f"<ThreadRouterProxy → {_active_router!r}>"


thread_router: ThreadRouter = cast("ThreadRouter", _ThreadRouterProxy())
