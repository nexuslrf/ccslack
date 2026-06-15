"""Authorisation helpers for Slack-side handlers.

Trust levels:

  1. **Global allow-list** — ``ALLOWED_USERS`` env var. Always authorised, and
     required for the *meta channel* (creating sessions, archiving everything,
     listing every session, etc.) and cross-session actions.
  2. **Channel membership** (private mode only) — a Slack private channel that
     ccslack bound to a tmux window. ccslack decides who is invited, so a member
     of a bound channel is implicitly trusted.
  3. **Per-channel grant** — users added with ``/ccslack adduser``, stored on
     ``thread_router``. This is the *only* in-channel trust in **public mode**
     (``CCSLACK_PUBLIC_CHANNELS``), where membership can't be trusted because
     anyone in the workspace can join a public channel.

:func:`is_authorized` composes these; :func:`is_meta_authorized` always defers
strictly to the global allow-list.
"""

from __future__ import annotations

from ..config import config
from ..thread_router import thread_router


def is_authorized(user_id: str, channel_id: str) -> bool:
    """Return True iff ``user_id`` may drive the bot in ``channel_id``.

    ``channel_id`` may be empty (e.g. for events without a channel) — in that
    case we fall through to the global allow-list.

    Trust composition:
      * ``ALLOWED_USERS`` — always authorised, anywhere.
      * Private mode (default): membership in a bound channel grants access —
        the bot controls who is invited, so the invite *is* the authorisation.
      * Public mode (``CCSLACK_PUBLIC_CHANNELS``): membership is NOT trusted
        (anyone can join a public channel), so a bound channel only grants
        access to users explicitly added via ``/ccslack adduser``.
    """
    if not user_id:
        return False
    if config.is_user_allowed(user_id):
        return True
    if not channel_id:
        return False
    if not config.public_channels and thread_router.has_channel(channel_id):
        return True
    return thread_router.is_user_granted(channel_id, user_id)


def is_meta_authorized(user_id: str) -> bool:
    """Stricter check used for slash-command paths that *only* exist in the
    meta channel (``/ccslack new``, ``/ccslack list``, ``/ccslack sessions``).

    Always defers to the global allow-list — never relaxed by channel
    membership.
    """
    return bool(user_id) and config.is_user_allowed(user_id)


__all__ = ["is_authorized", "is_meta_authorized"]
