"""Spawn request data types and file-based CRUD.

Pure functions for spawn request creation, rate limiting, and validation.
No handler/Telegram/config dependencies — safe for CLI use without bot token.

Key components:
  - SpawnRequest: dataclass for pending spawn requests
  - create_spawn_request: validate and store a new request
  - scan_spawn_requests: read pending requests from disk (for broker)
  - check_spawn_rate / check_max_windows: rate limiting helpers
"""

from __future__ import annotations

import contextlib
import json
import time
import uuid
from collections.abc import Iterator
from dataclasses import asdict, dataclass, field
from pathlib import Path

import structlog

from .topic_state_registry import topic_state
from .utils import atomic_write_json, ccslack_dir

logger = structlog.get_logger()

_SPAWN_RATE_WINDOW_SECONDS = 3600  # 1 hour


@dataclass
class SpawnRequest:
    id: str
    requester_window: str
    provider: str
    cwd: str
    prompt: str
    context_file: str | None = None
    auto: bool = False
    created_at: float = field(default_factory=time.time)

    def is_expired(self, timeout: int = 300) -> bool:
        return time.time() - self.created_at > timeout

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> SpawnRequest:
        return cls(
            id=data["id"],
            requester_window=data["requester_window"],
            provider=data.get("provider", "claude"),
            cwd=data["cwd"],
            prompt=data.get("prompt", ""),
            context_file=data.get("context_file"),
            auto=data.get("auto", False),
            created_at=data.get("created_at", 0.0),
        )


@dataclass
class SpawnResult:
    window_id: str
    window_name: str


# In-memory cache of requests loaded from disk (bot process only).
_pending_requests: dict[str, SpawnRequest] = {}


def spawns_dir() -> Path:
    return ccslack_dir() / "mailbox" / "spawns"


def get_pending(request_id: str) -> SpawnRequest | None:
    return _pending_requests.get(request_id)


def pop_pending(request_id: str) -> SpawnRequest | None:
    return _pending_requests.pop(request_id, None)


def iter_pending() -> Iterator[tuple[str, SpawnRequest]]:
    yield from _pending_requests.items()


def register_pending(req: SpawnRequest) -> None:
    _pending_requests[req.id] = req


def reset_spawn_state() -> None:
    _pending_requests.clear()


@topic_state.register("qualified")
def clear_spawn_state(window_id: str) -> None:
    to_remove = [
        rid
        for rid, req in _pending_requests.items()
        if req.requester_window == window_id
    ]
    for rid in to_remove:
        del _pending_requests[rid]
    sdir = spawns_dir()
    if sdir.is_dir():
        for entry in sdir.iterdir():
            if not entry.name.endswith(".json"):
                continue
            try:
                data = json.loads(entry.read_text())
                if data.get("requester_window") == window_id:
                    entry.unlink(missing_ok=True)
            except json.JSONDecodeError, OSError:
                continue


def check_max_windows(
    window_states: dict,
    max_windows: int,
) -> bool:
    return len(window_states) < max_windows


def _load_spawn_log() -> dict[str, list[float]]:
    """Load spawn rate log from disk."""
    path = spawns_dir() / "rate_log.json"
    if path.exists():
        try:
            return json.loads(path.read_text())
        except json.JSONDecodeError, OSError:
            return {}
    return {}


def _save_spawn_log(log: dict[str, list[float]]) -> None:
    """Save spawn rate log to disk."""
    sdir = spawns_dir()
    sdir.mkdir(parents=True, exist_ok=True)
    path = sdir / "rate_log.json"
    atomic_write_json(path, log)


def check_spawn_rate(window_id: str, max_rate: int) -> bool:
    log = _load_spawn_log()
    cutoff = time.time() - _SPAWN_RATE_WINDOW_SECONDS
    timestamps = log.get(window_id, [])
    recent = [t for t in timestamps if t > cutoff]
    return len(recent) < max_rate


def record_spawn(window_id: str) -> None:
    log = _load_spawn_log()
    log.setdefault(window_id, []).append(time.time())
    cutoff = time.time() - _SPAWN_RATE_WINDOW_SECONDS
    for wid in log:
        log[wid] = [t for t in log[wid] if t > cutoff]
    _save_spawn_log(log)


def create_spawn_request(
    requester_window: str,
    provider: str,
    cwd: str,
    prompt: str,
    context_file: str | None = None,
    auto: bool = False,
) -> SpawnRequest:
    if not Path(cwd).is_dir():
        raise ValueError(f"cwd does not exist: {cwd}")

    request_id = f"{int(time.time())}-{uuid.uuid4().hex[:8]}"
    req = SpawnRequest(
        id=request_id,
        requester_window=requester_window,
        provider=provider,
        cwd=cwd,
        prompt=prompt,
        context_file=context_file,
        auto=auto,
    )

    register_pending(req)
    sdir = spawns_dir()
    sdir.mkdir(parents=True, exist_ok=True)
    atomic_write_json(sdir / f"{request_id}.json", req.to_dict())

    record_spawn(requester_window)

    return req


def scan_spawn_requests(spawn_timeout: int = 300) -> list[SpawnRequest]:
    """Read pending spawn request files from disk.

    Called by the broker cycle in the bot process. Loads new requests
    into ``_pending_requests`` and returns them for keyboard posting.
    Also evicts expired cached requests so they don't remain approvable
    indefinitely.
    """
    sdir = spawns_dir()

    # Evict expired requests from the in-memory cache (and clean up files).
    for rid in list(_pending_requests):
        if _pending_requests[rid].is_expired(timeout=spawn_timeout):
            _pending_requests.pop(rid, None)
            if sdir.is_dir():
                spawn_file = sdir / f"{rid}.json"
                with contextlib.suppress(OSError):
                    spawn_file.unlink()

    if not sdir.is_dir():
        return []

    new_requests: list[SpawnRequest] = []
    for entry in sdir.iterdir():
        if not entry.name.endswith(".json") or entry.name == "rate_log.json":
            continue
        try:
            data = json.loads(entry.read_text())
            req = SpawnRequest.from_dict(data)
        except json.JSONDecodeError, OSError, KeyError:
            continue

        if req.id in _pending_requests:
            continue

        if req.is_expired(timeout=spawn_timeout):
            with contextlib.suppress(OSError):
                entry.unlink()
            continue

        register_pending(req)
        new_requests.append(req)

    return new_requests
