"""Decode Slack's inbound message encoding back to plain text.

Slack does not deliver message text verbatim. Before an event reaches the bot
it rewrites the body:

  * the user's literal ``&``, ``<``, ``>`` are escaped to ``&amp;``, ``&lt;``,
    ``&gt;``;
  * URLs are auto-linked and wrapped in angle brackets — ``<https://x>`` or
    ``<https://x|label>``;
  * mentions are wrapped too — ``<@U123|alice>``, ``<#C123|general>``,
    ``<!here>``, ``<!subteam^S1|@team>``, ``<!date^…|fallback>``.

Forwarded verbatim into a tmux shell, the angle brackets of an auto-linked URL
are read as redirection operators (``git clone <https://…>`` →
``syntax error near unexpected token `newline'``), and mentions/entities are
unreadable to a CLI agent. :func:`decode_slack_text` inverts the encoding so
what reaches the agent is what the human typed.

Public API:
  - ``decode_slack_text(text)`` — Slack message body → plain text.
"""

from __future__ import annotations

import re

# One angle-bracket span. Slack only produces well-formed, non-nested spans, so
# "everything up to the next '>'" is a safe capture.
_SPAN_RE = re.compile(r"<([^<>]+)>")


def _decode_span(inner: str) -> str:
    """Decode the contents of a single ``<…>`` span into plain text."""
    body, sep, display = inner.partition("|")
    display = display.strip()

    if body.startswith("@"):  # user mention: <@U123> / <@U123|alice>
        return f"@{display}" if display else body
    if body.startswith("#"):  # channel: <#C123|general>
        return f"#{display}" if display else body
    if body.startswith("!"):  # special: <!here>, <!subteam^S1|@team>, <!date^…|txt>
        if display:
            return display
        # <!here> / <!channel> / <!everyone> → @here etc.; strip any ^-args.
        return f"@{body[1:].split('^', 1)[0]}"
    if body.startswith("mailto:"):  # <mailto:a@b.com|a@b.com>
        return display or body[len("mailto:") :]
    # Plain link: <url> or <url|label>. The URL (before '|') is the actionable
    # part for a shell/agent, so drop the label.
    return body if sep else inner


def decode_slack_text(text: str) -> str:
    """Convert a Slack inbound message body to plain text.

    Unwraps Slack's ``<…>`` link/mention spans first (they use *real* angle
    brackets), then unescapes the three HTML entities Slack encodes. Order
    matters: doing spans first means a user's literal ``a < b`` (delivered as
    ``a &lt; b``) is never mistaken for a link, and entities *inside* a URL
    (e.g. ``&amp;`` in a query string) are still restored.
    """
    if not text:
        return text
    unwrapped = _SPAN_RE.sub(lambda m: _decode_span(m.group(1)), text)
    return (
        unwrapped.replace("&lt;", "<").replace("&gt;", ">").replace("&amp;", "&")
    )


__all__ = ["decode_slack_text"]
