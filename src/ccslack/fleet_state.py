"""Optional fleet view for meta handlers (decouples handlers from the router).

In a multi-host deployment the router process installs its
:class:`~ccslack.router.Router` here; meta handlers (``/ccslack new --host``,
``/ccslack list``) consult it to know the available hosts and remote sessions.
In standalone / worker processes nothing is installed and the accessors degrade
to "just this host, no remote sessions", so the handlers behave exactly as
before.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING

from .config import config

if TYPE_CHECKING:
    from .router import Router

_router: Router | None = None
# Configured workers as (host, ssh_target) — lets fleet_status show hosts that
# are configured but currently disconnected.
_workers: list[tuple[str, str]] = []
# Async callable returning remote session rows (set by the router to the fleet's
# gather_sessions). None when not a router.
_session_gatherer: Callable[[], Awaitable[list[dict[str, object]]]] | None = None


def install_router(router: Router) -> None:
    """Register the local Router (called once by the router process)."""
    global _router
    _router = router


def set_workers(workers: list[tuple[str, str]]) -> None:
    """Register the configured worker (host, ssh_target) list (router process)."""
    global _workers
    _workers = list(workers)


def set_session_gatherer(
    gatherer: Callable[[], Awaitable[list[dict[str, object]]]],
) -> None:
    """Register the coroutine that gathers remote workers' sessions (router)."""
    global _session_gatherer
    _session_gatherer = gatherer


async def remote_sessions() -> list[dict[str, object]]:
    """Gather connected workers' session rows (each tagged ``host``). [] if not fleet."""
    if _session_gatherer is None:
        return []
    return await _session_gatherer()


def reset() -> None:
    """Clear installed state (test isolation)."""
    global _router, _workers, _session_gatherer
    _router = None
    _workers = []
    _session_gatherer = None


def is_fleet() -> bool:
    """True when this process is a router with the fleet view installed."""
    return _router is not None


def hosts() -> list[str]:
    """All hosts that can run sessions: this host + connected remote workers."""
    local = config.host_name
    if _router is None:
        return [local]
    return sorted({local, *_router.connected_hosts})


def remote_channels() -> dict[str, str]:
    """``channel_id -> host`` for channels owned by *other* hosts (empty if not fleet)."""
    if _router is None:
        return {}
    return {
        channel: host
        for channel, host in _router.channel_host_items()
        if host != config.host_name
    }


async def forward(host: str, payload: dict[str, object]) -> bool:
    """Forward a raw Slack payload to *host*'s worker. False if not a router."""
    if _router is None:
        return False
    return await _router.forward(host, payload)


def fleet_status() -> list[dict[str, object]]:
    """Per-host status rows for ``/ccslack fleet`` (empty when not a router).

    Row: ``{host, role, connected, sessions, ssh}``. The local host (the router
    itself) is always connected; configured workers show even when disconnected.
    """
    if _router is None:
        return []
    # Lazy: thread_router is wired by SessionManager.
    from .thread_router import thread_router

    counts: dict[str, int] = {}
    for _channel, host in _router.channel_host_items():
        counts[host] = counts.get(host, 0) + 1

    rows: list[dict[str, object]] = [
        {
            "host": config.host_name,
            "role": "router",
            "connected": True,
            "sessions": len(thread_router.channel_bindings),
            "ssh": "",
        }
    ]
    for host, ssh_target in _workers:
        rows.append(
            {
                "host": host,
                "role": "worker",
                "connected": host in _router.connected_hosts,
                "sessions": counts.get(host, 0),
                "ssh": ssh_target,
            }
        )
    return rows


__all__ = [
    "fleet_status",
    "forward",
    "hosts",
    "install_router",
    "is_fleet",
    "remote_channels",
    "remote_sessions",
    "reset",
    "set_session_gatherer",
    "set_workers",
]
