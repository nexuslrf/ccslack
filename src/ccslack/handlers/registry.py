"""Bolt handler registration.

Walking-skeleton scope:
  - ``app_mention`` in the meta channel → "ccslack online" status reply.
  - ``message`` in bound session channels → forward to tmux (TODO: skeleton).
  - ``/ccslack new`` slash command → opens the new-session modal (TODO).

Feature subpackages (``handlers/meta.py``, ``handlers/text.py``, …) hang their
``register(app)`` functions off the registry and are wired here in dependency
order.
"""

from __future__ import annotations

import structlog
from typing import TYPE_CHECKING

from ..config import config

if TYPE_CHECKING:
    from slack_bolt.async_app import AsyncApp

logger = structlog.get_logger()


def register_all(app: AsyncApp) -> None:
    """Register every handler with the Bolt app."""
    _register_health_handlers(app)
    # Lazy: feature subpackages land incrementally.
    # Lazy: optional handler modules — absent ones are silently skipped.
    for name in (
        "meta",
        "text",
        "status",
        "screenshot",
        "toolbar",
        "interactive",
        "new_modal",
        "resume",
        "send",
        "recovery",
        "table_render",
        "purge",
    ):
        try:
            module = __import__(f"ccslack.handlers.{name}", fromlist=["register"])
        except ImportError:
            logger.debug("handlers.%s not present yet", name)
            continue
        register_fn = getattr(module, "register", None)
        if register_fn is None:
            logger.debug("handlers.%s has no register()", name)
            continue
        register_fn(app)


def _register_health_handlers(app: AsyncApp) -> None:
    """Bolt handlers that confirm the bot is alive."""

    @app.event("app_mention")
    async def on_app_mention(event: dict, say) -> None:  # noqa: ANN001
        """Respond to @ccslack in the meta channel with a status ping."""
        # Lazy: keep the import here so the registry stays a thin top-level wire.
        from .auth import is_authorized

        channel = event.get("channel", "")
        user = event.get("user", "")
        if not is_authorized(user, channel):
            logger.info("Ignoring app_mention from unauthorized user %s", user)
            return
        if channel != config.meta_channel_id:
            await say(
                channel=channel,
                text=":wave: I'm here — use the meta channel for `/ccslack` commands.",
            )
            return
        await say(channel=channel, text=":green_heart: ccslack online")
