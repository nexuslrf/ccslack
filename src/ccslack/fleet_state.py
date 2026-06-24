"""Optional fleet view for meta handlers (decouples handlers from the router).

In a multi-host deployment the router process installs its
:class:`~ccslack.router.Router` here; meta handlers (``/ccslack new --host``,
``/ccslack list``) consult it to know the available hosts and remote sessions.
In standalone / worker processes nothing is installed and the accessors degrade
to "just this host, no remote sessions", so the handlers behave exactly as
before.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from .config import config

if TYPE_CHECKING:
    from .router import Router

_router: Router | None = None


def install_router(router: Router) -> None:
    """Register the local Router (called once by the router process)."""
    global _router
    _router = router


def reset() -> None:
    """Clear installed state (test isolation)."""
    global _router
    _router = None


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


__all__ = [
    "hosts",
    "install_router",
    "is_fleet",
    "remote_channels",
    "reset",
]
