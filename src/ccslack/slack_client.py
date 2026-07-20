"""SlackClient Protocol + adapters.

Protocol exposing exactly the Slack Web API surface used across handlers and
top-level modules. Lets handlers depend on a narrow seam instead of importing
``slack_sdk.web.async_client.AsyncWebClient`` directly, so:

  - tests can pass a ``FakeSlackClient`` that records calls
  - ``BoltSlackClient`` wraps a real ``AsyncWebClient`` for production

Method names follow ``slack_sdk`` conventions (``chat_postMessage``,
``conversations_create``, …) so the adapter is straight delegation.

Public API:
  - ``SlackClient``       — Protocol the handlers depend on
  - ``BoltSlackClient``   — adapter wrapping ``AsyncWebClient``
  - ``FakeSlackClient``   — recording fake for tests
  - ``unwrap_web_client`` — escape hatch for SDK-only helpers
"""

from __future__ import annotations

import inspect
from dataclasses import dataclass, field
from typing import Any, Protocol, cast, runtime_checkable

from slack_sdk.web.async_client import AsyncWebClient
from slack_sdk.web.async_slack_response import AsyncSlackResponse


@runtime_checkable
class SlackClient(Protocol):
    """Narrow seam over the ``AsyncWebClient`` methods used in this codebase.

    Each method mirrors the corresponding ``AsyncWebClient`` method's name and
    primary arguments. ``**kwargs`` is accepted on every method so callers can
    pass additional SDK-supported parameters without forcing this Protocol to
    enumerate them all.
    """

    # --- chat ---------------------------------------------------------

    async def chat_postMessage(
        self, *, channel: str, text: str = "", **kwargs: Any
    ) -> AsyncSlackResponse: ...

    async def chat_update(
        self, *, channel: str, ts: str, text: str = "", **kwargs: Any
    ) -> AsyncSlackResponse: ...

    async def chat_delete(
        self, *, channel: str, ts: str, **kwargs: Any
    ) -> AsyncSlackResponse: ...

    async def chat_postEphemeral(
        self, *, channel: str, user: str, text: str = "", **kwargs: Any
    ) -> AsyncSlackResponse: ...

    # --- conversations -----------------------------------------------

    async def conversations_create(
        self, *, name: str, is_private: bool = True, **kwargs: Any
    ) -> AsyncSlackResponse: ...

    async def conversations_archive(
        self, *, channel: str, **kwargs: Any
    ) -> AsyncSlackResponse: ...

    async def conversations_unarchive(
        self, *, channel: str, **kwargs: Any
    ) -> AsyncSlackResponse: ...

    async def conversations_invite(
        self, *, channel: str, users: str, **kwargs: Any
    ) -> AsyncSlackResponse: ...

    async def conversations_info(
        self, *, channel: str, **kwargs: Any
    ) -> AsyncSlackResponse: ...

    async def conversations_setTopic(
        self, *, channel: str, topic: str, **kwargs: Any
    ) -> AsyncSlackResponse: ...

    async def conversations_setPurpose(
        self, *, channel: str, purpose: str, **kwargs: Any
    ) -> AsyncSlackResponse: ...

    async def conversations_rename(
        self, *, channel: str, name: str, **kwargs: Any
    ) -> AsyncSlackResponse: ...

    # --- pins ---------------------------------------------------------

    async def pins_add(
        self, *, channel: str, timestamp: str, **kwargs: Any
    ) -> AsyncSlackResponse: ...

    async def pins_remove(
        self, *, channel: str, timestamp: str, **kwargs: Any
    ) -> AsyncSlackResponse: ...

    # --- files --------------------------------------------------------

    async def files_upload_v2(
        self,
        *,
        channel: str | None = None,
        file: Any = None,
        filename: str | None = None,
        title: str | None = None,
        initial_comment: str | None = None,
        **kwargs: Any,
    ) -> AsyncSlackResponse: ...

    async def files_delete(self, *, file: str, **kwargs: Any) -> AsyncSlackResponse: ...

    # --- views (modals) ----------------------------------------------

    async def views_open(
        self, *, trigger_id: str, view: dict[str, Any], **kwargs: Any
    ) -> AsyncSlackResponse: ...

    async def views_update(
        self, *, view_id: str, view: dict[str, Any], **kwargs: Any
    ) -> AsyncSlackResponse: ...

    async def views_push(
        self, *, trigger_id: str, view: dict[str, Any], **kwargs: Any
    ) -> AsyncSlackResponse: ...

    # --- reactions ---------------------------------------------------

    async def reactions_add(
        self, *, channel: str, name: str, timestamp: str, **kwargs: Any
    ) -> AsyncSlackResponse: ...

    async def reactions_remove(
        self, *, channel: str, name: str, timestamp: str, **kwargs: Any
    ) -> AsyncSlackResponse: ...

    # --- users --------------------------------------------------------

    async def users_info(self, *, user: str, **kwargs: Any) -> AsyncSlackResponse: ...


class BoltSlackClient:
    """Adapter that delegates ``SlackClient`` calls to a ``AsyncWebClient``.

    Constructed once per process from ``app.client`` in ``bootstrap`` and
    threaded into handlers as a ``SlackClient``.
    """

    def __init__(self, web_client: AsyncWebClient) -> None:
        self._web = web_client

    @property
    def web_client(self) -> AsyncWebClient:
        """Underlying ``AsyncWebClient`` — escape hatch for SDK-only helpers."""
        return self._web

    async def chat_postMessage(
        self, *, channel: str, text: str = "", **kwargs: Any
    ) -> AsyncSlackResponse:
        return await self._web.chat_postMessage(channel=channel, text=text, **kwargs)

    async def chat_update(
        self, *, channel: str, ts: str, text: str = "", **kwargs: Any
    ) -> AsyncSlackResponse:
        return await self._web.chat_update(channel=channel, ts=ts, text=text, **kwargs)

    async def chat_delete(
        self, *, channel: str, ts: str, **kwargs: Any
    ) -> AsyncSlackResponse:
        return await self._web.chat_delete(channel=channel, ts=ts, **kwargs)

    async def chat_postEphemeral(
        self, *, channel: str, user: str, text: str = "", **kwargs: Any
    ) -> AsyncSlackResponse:
        return await self._web.chat_postEphemeral(
            channel=channel, user=user, text=text, **kwargs
        )

    async def conversations_create(
        self, *, name: str, is_private: bool = True, **kwargs: Any
    ) -> AsyncSlackResponse:
        return await self._web.conversations_create(
            name=name, is_private=is_private, **kwargs
        )

    async def conversations_archive(
        self, *, channel: str, **kwargs: Any
    ) -> AsyncSlackResponse:
        return await self._web.conversations_archive(channel=channel, **kwargs)

    async def conversations_unarchive(
        self, *, channel: str, **kwargs: Any
    ) -> AsyncSlackResponse:
        return await self._web.conversations_unarchive(channel=channel, **kwargs)

    async def conversations_invite(
        self, *, channel: str, users: str, **kwargs: Any
    ) -> AsyncSlackResponse:
        return await self._web.conversations_invite(
            channel=channel, users=users, **kwargs
        )

    async def conversations_info(
        self, *, channel: str, **kwargs: Any
    ) -> AsyncSlackResponse:
        return await self._web.conversations_info(channel=channel, **kwargs)

    async def conversations_setTopic(
        self, *, channel: str, topic: str, **kwargs: Any
    ) -> AsyncSlackResponse:
        return await self._web.conversations_setTopic(
            channel=channel, topic=topic, **kwargs
        )

    async def conversations_setPurpose(
        self, *, channel: str, purpose: str, **kwargs: Any
    ) -> AsyncSlackResponse:
        return await self._web.conversations_setPurpose(
            channel=channel, purpose=purpose, **kwargs
        )

    async def conversations_rename(
        self, *, channel: str, name: str, **kwargs: Any
    ) -> AsyncSlackResponse:
        return await self._web.conversations_rename(
            channel=channel, name=name, **kwargs
        )

    async def pins_add(
        self, *, channel: str, timestamp: str, **kwargs: Any
    ) -> AsyncSlackResponse:
        return await self._web.pins_add(channel=channel, timestamp=timestamp, **kwargs)

    async def pins_remove(
        self, *, channel: str, timestamp: str, **kwargs: Any
    ) -> AsyncSlackResponse:
        return await self._web.pins_remove(
            channel=channel, timestamp=timestamp, **kwargs
        )

    async def files_upload_v2(
        self,
        *,
        channel: str | None = None,
        file: Any = None,
        filename: str | None = None,
        title: str | None = None,
        initial_comment: str | None = None,
        **kwargs: Any,
    ) -> AsyncSlackResponse:
        return await self._web.files_upload_v2(
            channel=channel,
            file=file,
            filename=filename,
            title=title,
            initial_comment=initial_comment,
            **kwargs,
        )

    async def files_delete(self, *, file: str, **kwargs: Any) -> AsyncSlackResponse:
        return await self._web.files_delete(file=file, **kwargs)

    async def views_open(
        self, *, trigger_id: str, view: dict[str, Any], **kwargs: Any
    ) -> AsyncSlackResponse:
        return await self._web.views_open(trigger_id=trigger_id, view=view, **kwargs)

    async def views_update(
        self, *, view_id: str, view: dict[str, Any], **kwargs: Any
    ) -> AsyncSlackResponse:
        return await self._web.views_update(view_id=view_id, view=view, **kwargs)

    async def views_push(
        self, *, trigger_id: str, view: dict[str, Any], **kwargs: Any
    ) -> AsyncSlackResponse:
        return await self._web.views_push(trigger_id=trigger_id, view=view, **kwargs)

    async def reactions_add(
        self, *, channel: str, name: str, timestamp: str, **kwargs: Any
    ) -> AsyncSlackResponse:
        return await self._web.reactions_add(
            channel=channel, name=name, timestamp=timestamp, **kwargs
        )

    async def reactions_remove(
        self, *, channel: str, name: str, timestamp: str, **kwargs: Any
    ) -> AsyncSlackResponse:
        return await self._web.reactions_remove(
            channel=channel, name=name, timestamp=timestamp, **kwargs
        )

    async def users_info(self, *, user: str, **kwargs: Any) -> AsyncSlackResponse:
        return await self._web.users_info(user=user, **kwargs)


@dataclass
class _FakeCall:
    """Single recorded call on ``FakeSlackClient``."""

    method: str
    kwargs: dict[str, Any]


@dataclass
class FakeSlackClient:
    """Recording fake for tests.

    Every ``await client.chat_postMessage(...)`` records an entry on ``calls``.
    Tests can pre-seed ``returns[method_name]`` with a callable producing the
    return value, or rely on the default (a truthy mapping).

    Deliberately *not* a subclass of ``BoltSlackClient`` — duck-typing to
    ``SlackClient`` keeps the seam honest.
    """

    calls: list[_FakeCall] = field(default_factory=list)
    returns: dict[str, Any] = field(default_factory=dict)

    def _record(self, method: str, kwargs: dict[str, Any]) -> Any:
        self.calls.append(_FakeCall(method=method, kwargs=dict(kwargs)))
        if method in self.returns:
            spec = self.returns[method]
            if inspect.isfunction(spec) or inspect.ismethod(spec):
                return spec(**kwargs)
            return spec
        return _DEFAULT_RETURNS.get(method, {"ok": True})

    def call_count(self, method: str) -> int:
        return sum(1 for c in self.calls if c.method == method)

    def last_call(self, method: str) -> _FakeCall | None:
        for call in reversed(self.calls):
            if call.method == method:
                return call
        return None

    def set_side_effect(self, method: str, effects: list[Any]) -> None:
        """Queue per-call return values / exceptions for ``method``.

        Each element of ``effects`` is consumed in order on successive calls:

          * An ``Exception`` instance — raised by the method.
          * Anything else — returned as the method's value.

        Mirrors ``unittest.mock.Mock.side_effect`` for the iterable case.
        """
        iterator = iter(effects)

        def step(**_kwargs: Any) -> Any:
            value = next(iterator)
            if isinstance(value, BaseException):
                raise value
            return value

        self.returns[method] = step

    async def chat_postMessage(
        self, *, channel: str, text: str = "", **kwargs: Any
    ) -> Any:
        return self._record(
            "chat_postMessage", {"channel": channel, "text": text, **kwargs}
        )

    async def chat_update(
        self, *, channel: str, ts: str, text: str = "", **kwargs: Any
    ) -> Any:
        return self._record(
            "chat_update", {"channel": channel, "ts": ts, "text": text, **kwargs}
        )

    async def chat_delete(self, *, channel: str, ts: str, **kwargs: Any) -> Any:
        return self._record("chat_delete", {"channel": channel, "ts": ts, **kwargs})

    async def chat_postEphemeral(
        self, *, channel: str, user: str, text: str = "", **kwargs: Any
    ) -> Any:
        return self._record(
            "chat_postEphemeral",
            {"channel": channel, "user": user, "text": text, **kwargs},
        )

    async def conversations_create(
        self, *, name: str, is_private: bool = True, **kwargs: Any
    ) -> Any:
        return self._record(
            "conversations_create", {"name": name, "is_private": is_private, **kwargs}
        )

    async def conversations_archive(self, *, channel: str, **kwargs: Any) -> Any:
        return self._record("conversations_archive", {"channel": channel, **kwargs})

    async def conversations_unarchive(self, *, channel: str, **kwargs: Any) -> Any:
        return self._record("conversations_unarchive", {"channel": channel, **kwargs})

    async def conversations_invite(
        self, *, channel: str, users: str, **kwargs: Any
    ) -> Any:
        return self._record(
            "conversations_invite", {"channel": channel, "users": users, **kwargs}
        )

    async def conversations_info(self, *, channel: str, **kwargs: Any) -> Any:
        return self._record("conversations_info", {"channel": channel, **kwargs})

    async def conversations_setTopic(
        self, *, channel: str, topic: str, **kwargs: Any
    ) -> Any:
        return self._record(
            "conversations_setTopic", {"channel": channel, "topic": topic, **kwargs}
        )

    async def conversations_setPurpose(
        self, *, channel: str, purpose: str, **kwargs: Any
    ) -> Any:
        return self._record(
            "conversations_setPurpose",
            {"channel": channel, "purpose": purpose, **kwargs},
        )

    async def conversations_rename(
        self, *, channel: str, name: str, **kwargs: Any
    ) -> Any:
        return self._record(
            "conversations_rename", {"channel": channel, "name": name, **kwargs}
        )

    async def pins_add(self, *, channel: str, timestamp: str, **kwargs: Any) -> Any:
        return self._record(
            "pins_add", {"channel": channel, "timestamp": timestamp, **kwargs}
        )

    async def pins_remove(self, *, channel: str, timestamp: str, **kwargs: Any) -> Any:
        return self._record(
            "pins_remove", {"channel": channel, "timestamp": timestamp, **kwargs}
        )

    async def files_upload_v2(
        self,
        *,
        channel: str | None = None,
        file: Any = None,
        filename: str | None = None,
        title: str | None = None,
        initial_comment: str | None = None,
        **kwargs: Any,
    ) -> Any:
        return self._record(
            "files_upload_v2",
            {
                "channel": channel,
                "file": file,
                "filename": filename,
                "title": title,
                "initial_comment": initial_comment,
                **kwargs,
            },
        )

    async def files_delete(self, *, file: str, **kwargs: Any) -> Any:
        return self._record("files_delete", {"file": file, **kwargs})

    async def views_open(
        self, *, trigger_id: str, view: dict[str, Any], **kwargs: Any
    ) -> Any:
        return self._record(
            "views_open", {"trigger_id": trigger_id, "view": view, **kwargs}
        )

    async def views_update(
        self, *, view_id: str, view: dict[str, Any], **kwargs: Any
    ) -> Any:
        return self._record(
            "views_update", {"view_id": view_id, "view": view, **kwargs}
        )

    async def views_push(
        self, *, trigger_id: str, view: dict[str, Any], **kwargs: Any
    ) -> Any:
        return self._record(
            "views_push", {"trigger_id": trigger_id, "view": view, **kwargs}
        )

    async def reactions_add(
        self, *, channel: str, name: str, timestamp: str, **kwargs: Any
    ) -> Any:
        return self._record(
            "reactions_add",
            {"channel": channel, "name": name, "timestamp": timestamp, **kwargs},
        )

    async def reactions_remove(
        self, *, channel: str, name: str, timestamp: str, **kwargs: Any
    ) -> Any:
        return self._record(
            "reactions_remove",
            {"channel": channel, "name": name, "timestamp": timestamp, **kwargs},
        )

    async def users_info(self, *, user: str, **kwargs: Any) -> Any:
        return self._record("users_info", {"user": user, **kwargs})


# Default returns for fakes — every call gets ``{"ok": True}`` unless overridden.
_DEFAULT_RETURNS: dict[str, Any] = {}


def unwrap_web_client(client: SlackClient) -> AsyncWebClient:
    """Return the underlying ``AsyncWebClient`` from a ``SlackClient``."""
    if isinstance(client, BoltSlackClient):
        return client.web_client
    return cast(AsyncWebClient, client)


__all__ = [
    "BoltSlackClient",
    "FakeSlackClient",
    "SlackClient",
    "unwrap_web_client",
]
