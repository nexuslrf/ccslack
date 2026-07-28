"""Detect files an agent answer refers to and offer a "Show files" button.

An agent's final answer often names files it created or edited (``src/foo.py``,
``` `report.md` ```, a markdown link). This module scans that answer for
path-like tokens, keeps only the ones that resolve to a real, sendable file
under the session cwd, and — when any survive — posts an opt-in button. On
click it lists exactly those files (not a folder browser) using ``send``'s
existing per-file picker, so tapping one uploads it through the same validated
path as ``/ccslack send``.

Pipeline:
  * ``find_file_refs(text, cwd)`` — path-like tokens → existing sendable files.
  * ``maybe_offer_file_refs``    — post the "Show files" prompt when any found.
  * ``register(app)``            — wire the Show files / Dismiss buttons.

Existence + ``validate_sendable`` (cwd containment, hidden/secret/gitleaks
guards) are the real filter, so the token regexes err toward over-capturing.
"""

from __future__ import annotations

import contextlib
import re
import structlog
import uuid
from pathlib import Path
from typing import TYPE_CHECKING

from slack_sdk.errors import SlackApiError

from ..config import config
from ..slack_sender import safe_post
from .send_security import is_hidden, is_path_contained, validate_sendable

if TYPE_CHECKING:
    from slack_bolt.async_app import AsyncApp

    from ..slack_client import SlackClient

logger = structlog.get_logger()

# Candidate path tokens come from three shapes; each is deliberately loose
# because the existence + security validation below does the real filtering.
_BACKTICK_RE = re.compile(r"`([^`\n]+)`")  # inline code span
_MDLINK_RE = re.compile(r"\]\(([^)\s]+)\)")  # markdown link target
# Slack auto-link markup: <target|label> or <target>. Agent paths that Slack
# rendered as links arrive this way — take the target (before the optional
# ``|label``), which carries the real path.
_SLACK_LINK_RE = re.compile(r"<([^>|\s]+)(?:\|[^>]*)?>")
# Bare path tokens, three shapes:
#   * absolute:  /a/b/c.py            (leading slash, preceded by a non-word char)
#   * relative:  src/foo.py, ~/a/b    (slash-bearing)
#   * bare file: report.md            (lone filename with an extension)
# Word boundaries keep them from eating prose; existence + validate_sendable is
# the real filter, so over-capturing is safe.
_BARE_RE = re.compile(
    r"(?<![\w])/(?:[\w.+\-]+/)*[\w.+\-]+"
    r"|(?<![\w/])~?(?:[\w.+\-]+/)+[\w.+\-]+"
    r"|(?<![\w/])[\w.+\-]+\.[A-Za-z0-9]{1,12}(?![\w])"
)

# A trailing ``:line`` or ``:line:col`` suffix editors/agents append to a path
# (``foo.py:722``, ``foo.py:722:5``) — stripped so the path itself resolves.
_LINE_SUFFIX_RE = re.compile(r":\d+(?::\d+)?$")

# Trailing punctuation that sentence context (not the filename) contributed.
_TRAILING = ".,;:!?)]}>\"'`"

# Cap how many files we surface — a huge answer shouldn't spawn an unbounded
# validation sweep (validate_sendable may shell out to gitleaks) or button wall.
_MAX_FILES = 20

# Pending list jobs keyed by a short token carried in the button value.
# token -> (channel_id, [abs_path_str]). Bounded to avoid unbounded growth.
_PENDING: dict[str, tuple[str, list[str]]] = {}
_PENDING_MAX = 256


def _clean_token(tok: str) -> str:
    """Strip quoting/wrapping, a ``:line`` suffix, and sentence punctuation."""
    tok = tok.strip().strip("`'\"<>")
    # Drop a trailing editor line/column ref (foo.py:722[:5]) before the
    # sentence-punctuation sweep, which wouldn't remove the digits.
    tok = _LINE_SUFFIX_RE.sub("", tok)
    while tok and tok[-1] in _TRAILING:
        tok = tok[:-1]
    if tok.startswith("./"):
        tok = tok[2:]
    return tok


def _candidates(text: str) -> list[str]:
    """Extract cleaned, de-duplicated path-like tokens from *text* (in order)."""
    raw: list[str] = []
    raw.extend(_BACKTICK_RE.findall(text))
    raw.extend(_MDLINK_RE.findall(text))
    raw.extend(_SLACK_LINK_RE.findall(text))
    raw.extend(_BARE_RE.findall(text))
    seen: set[str] = set()
    out: list[str] = []
    for tok in raw:
        cleaned = _clean_token(tok)
        # Need a filename-ish token; skip URLs and bare directories/words.
        if not cleaned or "://" in cleaned or "/" not in cleaned and "." not in cleaned:
            continue
        if cleaned not in seen:
            seen.add(cleaned)
            out.append(cleaned)
    return out


def _resolve(tok: str, cwd: Path) -> Path:
    """Resolve a token to an absolute path (relative tokens rooted at cwd)."""
    p = Path(tok).expanduser()
    return p if p.is_absolute() else cwd / p


def find_file_refs(text: str, cwd: Path) -> list[Path]:
    """Return real, sendable files referenced in *text*, capped at ``_MAX_FILES``.

    A token qualifies only if it resolves to an existing regular file inside
    *cwd* that passes the full ``validate_sendable`` pipeline (hidden/secret/
    gitleaks guards). De-duplicated by absolute path, preserving mention order.
    """
    found: list[Path] = []
    seen_abs: set[str] = set()
    for tok in _candidates(text):
        path = _resolve(tok, cwd)
        # Cheap gate first: must be an in-cwd, non-hidden regular file.
        try:
            if not path.is_file() or not is_path_contained(path, cwd) or is_hidden(
                path, cwd
            ):
                continue
        except OSError:
            continue
        key = str(path.resolve())
        if key in seen_abs:
            continue
        seen_abs.add(key)
        # Full security pipeline (secret patterns, gitleaks, state-file guard).
        if validate_sendable(path, cwd) is not None:
            continue
        found.append(path)
        if len(found) >= _MAX_FILES:
            break
    return found


def _remember(token: str, channel_id: str, paths: list[Path]) -> None:
    if len(_PENDING) >= _PENDING_MAX:
        with contextlib.suppress(StopIteration):
            del _PENDING[next(iter(_PENDING))]
    _PENDING[token] = (channel_id, [str(p.resolve()) for p in paths])


async def maybe_offer_file_refs(
    client: SlackClient, channel_id: str, text: str
) -> None:
    """Post a "Show files" prompt when *text* references sendable files."""
    if not config.file_refs_offer:
        return
    from .send import _resolve_cwd

    cwd = _resolve_cwd(channel_id)
    if cwd is None:
        return
    paths = find_file_refs(text, cwd)
    if not paths:
        return
    token = uuid.uuid4().hex[:12]
    _remember(token, channel_id, paths)
    count = len(paths)
    label = "1 file" if count == 1 else f"{count} files"
    await safe_post(
        client,
        channel=channel_id,
        text=f":open_file_folder: The message above references {label}.",
        blocks=[
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": (
                        f":open_file_folder: The message above references "
                        f"*{label}* in this project. List them for one-tap upload?"
                    ),
                },
            },
            {
                "type": "actions",
                "block_id": f"ccslack_file_refs_actions:{token}",
                "elements": [
                    {
                        "type": "button",
                        "action_id": "ccslack_show_files",
                        "style": "primary",
                        "text": {"type": "plain_text", "text": ":page_facing_up: Show files"},
                        "value": token,
                    },
                    {
                        "type": "button",
                        "action_id": "ccslack_show_files_dismiss",
                        "text": {"type": "plain_text", "text": ":x: Dismiss"},
                        "value": token,
                    },
                ],
            },
        ],
    )


def _button_token(body: dict, action_id: str) -> str:
    for action in body.get("actions", []) or []:
        if action.get("action_id") == action_id:
            return action.get("value", "")
    return ""


def register(app: AsyncApp) -> None:
    """Wire the Show files / Dismiss button actions."""

    @app.action("ccslack_show_files")
    async def on_show(ack, body, client) -> None:  # noqa: ANN001
        await ack()
        user_id = body.get("user", {}).get("id", "")
        channel_id = body.get("channel", {}).get("id", "")
        from .auth import is_authorized

        if not is_authorized(user_id, channel_id) or not channel_id:
            return
        token = _button_token(body, "ccslack_show_files")
        # Keep the offer live (ephemeral listing is idempotent and per-user, so
        # other members can still list); the token only expires via LRU.
        entry = _PENDING.get(token)
        if entry is None:
            with contextlib.suppress(SlackApiError):
                await client.chat_postEphemeral(
                    channel=channel_id,
                    user=user_id,
                    text="ccslack: this file list has expired.",
                )
            return

        from .send import _post_picker, _resolve_cwd

        cwd = _resolve_cwd(channel_id)
        if cwd is None:
            return
        # Re-check existence at click time; a file may have been removed since.
        paths = [Path(p) for p in entry[1] if Path(p).is_file()]
        if not paths:
            with contextlib.suppress(SlackApiError):
                await client.chat_postEphemeral(
                    channel=channel_id,
                    user=user_id,
                    text="ccslack: those files are no longer present.",
                )
            return
        await _post_picker(client, channel_id, user_id, paths, cwd)

    @app.action("ccslack_show_files_dismiss")
    async def on_dismiss(ack, body, client) -> None:  # noqa: ANN001
        await ack()
        channel_id = body.get("channel", {}).get("id", "")
        message_ts = (body.get("message") or {}).get("ts", "")
        token = _button_token(body, "ccslack_show_files_dismiss")
        _PENDING.pop(token, None)
        if message_ts and channel_id:
            with contextlib.suppress(SlackApiError):
                await client.chat_delete(channel=channel_id, ts=message_ts)


__all__ = [
    "find_file_refs",
    "maybe_offer_file_refs",
    "register",
]
