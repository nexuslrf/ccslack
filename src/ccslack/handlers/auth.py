"""Authorisation helpers for Slack-side handlers.

Two distinct trust levels:

  1. **Global allow-list** — ``ALLOWED_USERS`` env var. Required to invoke any
     bot action in the *meta channel* (creating sessions, archiving everything,
     listing every session, etc.).
  2. **Channel membership** — a Slack private channel that ccslack has bound
     to a tmux window. ccslack itself decides who gets invited to those
     channels (via the new-session flow); Slack guarantees only members can
     send messages / click buttons inside. So if an event arrives from a bound
     channel, the user is implicitly trusted.

:func:`is_authorized` composes the two: bound-channel members pass without
being on the global list; anything outside a bound channel falls back to the
whitelist.
"""

from __future__ import annotations

from ..config import config
from ..thread_router import thread_router


def is_authorized(user_id: str, channel_id: str) -> bool:
    """Return True iff ``user_id`` may drive the bot in ``channel_id``.

    ``channel_id`` may be empty (e.g. for events without a channel) — in that
    case we fall through to the global allow-list.
    """
    if not user_id:
        return False
    if channel_id and thread_router.has_channel(channel_id):
        # Membership in a bound private session channel grants full access —
        # the bot itself invited every member, so the invite act *is* the
        # authorisation.
        return True
    return config.is_user_allowed(user_id)


def is_meta_authorized(user_id: str) -> bool:
    """Stricter check used for slash-command paths that *only* exist in the
    meta channel (``/ccslack new``, ``/ccslack list``, ``/ccslack sessions``).

    Always defers to the global allow-list — never relaxed by channel
    membership.
    """
    return bool(user_id) and config.is_user_allowed(user_id)


__all__ = ["is_authorized", "is_meta_authorized"]
