"""Outbound message pipeline — transcripts → Slack channel.

Walking-skeleton scope: no per-channel queue, no tool-use pairing, no
notification modes. Just route ``NewMessage`` events from ``SessionMonitor`` to
the bound Slack channel via ``safe_post``.
"""

from .message_routing import handle_new_message

__all__ = ["handle_new_message"]
