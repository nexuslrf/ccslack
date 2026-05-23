"""Per-provider action toolbar — Block Kit buttons that drive the tmux TUI.

ccgram has a ``/toolbar`` command that renders a 3×3 inline-keyboard grid;
ccslack ports the same idea to Block Kit ``actions`` blocks. Each button
maps to a tmux key sequence (Esc, Tab, arrows, Ctrl-C, digits 1–9, …) sent
to the bound window via ``tmux_manager.send_keys(..., literal=False)``.

Flow:
  * Pinned status message has a ``🎛️ Toolbar`` button.
  * Clicking it posts a new toolbar message in the channel with the
    provider-specific button layout (Claude / Codex / Gemini / Pi / Shell).
  * Each button click dispatches to ``_on_key`` which sends the named tmux
    key to the bound window. The toolbar message stays put so you can drive
    a picker (arrow keys + Enter) without re-clicking ``🎛️``.
  * A ``Close`` button deletes the toolbar message.

Action-id shape:

  * ``ccslack_key:<tmux-key>``    — value carries the ``window_id``.
  * ``ccslack_toolbar_close``     — value carries the message ts to delete.

The tmux key names match ``tmux send-keys`` syntax verbatim — see
https://man7.org/linux/man-pages/man1/tmux.1.html#KEY_BINDINGS.
"""

from __future__ import annotations

import contextlib
import structlog
from typing import TYPE_CHECKING, Any

from slack_sdk.errors import SlackApiError

from ..session import session_manager
from ..thread_router import thread_router
from ..tmux_manager import tmux_manager

if TYPE_CHECKING:
    from slack_bolt.async_app import AsyncApp

logger = structlog.get_logger()


# (display, tmux_key) — display goes on the button, tmux_key into send-keys.
_Layout = list[list[tuple[str, str]]]


_CLAUDE_LAYOUT: _Layout = [
    [
        (":arrow_left_hook: Esc", "Escape"),
        (":left_right_arrow: Mode", "BTab"),  # Shift+Tab cycles approval mode
        (":bulb: Think", "Tab"),
        (":octagonal_sign: ^C", "C-c"),
    ],
    [
        (":arrow_up: Up", "Up"),
        (":arrow_down: Down", "Down"),
        (":leftwards_arrow_with_hook: Enter", "Enter"),
        (":back: Bksp", "BSpace"),
    ],
    [
        ("1", "1"),
        ("2", "2"),
        ("3", "3"),
        ("4", "4"),
        ("5", "5"),
    ],
]

_CODEX_LAYOUT: _Layout = [
    [
        (":arrow_left_hook: Esc", "Escape"),
        (":arrow_right_hook: Tab", "Tab"),
        (":left_right_arrow: Model", "BTab"),
        (":octagonal_sign: ^C", "C-c"),
    ],
    [
        (":arrow_up: Up", "Up"),
        (":arrow_down: Down", "Down"),
        (":leftwards_arrow_with_hook: Enter", "Enter"),
        (":back: Bksp", "BSpace"),
    ],
    [
        ("1", "1"),
        ("2", "2"),
        ("3", "3"),
        ("4", "4"),
        ("5", "5"),
    ],
]

_GEMINI_LAYOUT: _Layout = _CODEX_LAYOUT
_PI_LAYOUT: _Layout = _CLAUDE_LAYOUT

_SHELL_LAYOUT: _Layout = [
    [
        (":leftwards_arrow_with_hook: Enter", "Enter"),
        (":octagonal_sign: ^C", "C-c"),
        (":eject: ^D EOF", "C-d"),
        (":zzz: ^Z Susp", "C-z"),
    ],
    [
        (":arrow_up: Up", "Up"),
        (":arrow_down: Down", "Down"),
        (":arrow_right_hook: Tab", "Tab"),
        (":arrow_left_hook: Esc", "Escape"),
    ],
]


_LAYOUTS: dict[str, _Layout] = {
    "claude": _CLAUDE_LAYOUT,
    "codex": _CODEX_LAYOUT,
    "gemini": _GEMINI_LAYOUT,
    "pi": _PI_LAYOUT,
    "shell": _SHELL_LAYOUT,
}


def _layout_for(provider: str) -> _Layout:
    return _LAYOUTS.get(provider.lower(), _CLAUDE_LAYOUT)


def build_toolbar_blocks(window_id: str) -> tuple[list[dict[str, Any]], str]:
    """Build the toolbar's Block Kit blocks for a window. Returns (blocks, fallback)."""
    view = session_manager.view_window(window_id)
    provider = (view.provider_name if view else "") or "claude"
    layout = _layout_for(provider)
    display = thread_router.get_display_name(window_id)

    blocks: list[dict[str, Any]] = [
        {
            "type": "context",
            "elements": [
                {
                    "type": "mrkdwn",
                    "text": (
                        f":control_knobs: *{provider}* toolbar · `{window_id}` "
                        f"({display}) — buttons send keys to the tmux pane."
                    ),
                }
            ],
        }
    ]
    for row in layout:
        blocks.append(
            {
                "type": "actions",
                "elements": [
                    {
                        "type": "button",
                        "action_id": f"ccslack_key:{tmux_key}",
                        "text": {"type": "plain_text", "text": label},
                        "value": window_id,
                    }
                    for label, tmux_key in row
                ],
            }
        )
    # Close row (separate so it isn't crammed with key buttons).
    blocks.append(
        {
            "type": "actions",
            "elements": [
                {
                    "type": "button",
                    "action_id": "ccslack_toolbar_close",
                    "style": "danger",
                    "text": {"type": "plain_text", "text": ":x: Close"},
                    "value": window_id,
                }
            ],
        }
    )
    fallback = f"{provider} toolbar for {window_id}"
    return blocks, fallback


async def open_toolbar(
    client,  # noqa: ANN001 — Bolt-provided AsyncWebClient
    channel_id: str,
    window_id: str,
) -> str | None:
    """Post the toolbar message in the channel. Returns the new ts."""
    blocks, fallback = build_toolbar_blocks(window_id)
    try:
        result = await client.chat_postMessage(
            channel=channel_id, text=fallback, blocks=blocks
        )
        return result.get("ts") if hasattr(result, "get") else result["ts"]
    except SlackApiError as exc:
        logger.warning(
            "toolbar post failed: %s",
            exc.response.get("error") if exc.response else exc,
        )
        return None


def register(app: AsyncApp) -> None:
    """Wire toolbar handlers: open from status bar, single-key dispatch, close."""

    @app.action("ccslack_toolbar_open")
    async def on_open(ack, body, client) -> None:  # noqa: ANN001
        await ack()
        user_id = body.get("user", {}).get("id", "")
        channel_id = body.get("channel", {}).get("id", "")
        # Lazy: auth helper at call site.
        from .auth import is_authorized

        if not is_authorized(user_id, channel_id) or not channel_id:
            return
        window_id = _extract_window_id(body, "ccslack_toolbar_open")
        if not window_id:
            return
        await open_toolbar(client, channel_id, window_id)

    @app.action("ccslack_toolbar_close")
    async def on_close(ack, body, client) -> None:  # noqa: ANN001
        await ack()
        user_id = body.get("user", {}).get("id", "")
        channel_id = body.get("channel", {}).get("id", "")
        from .auth import is_authorized

        if not is_authorized(user_id, channel_id) or not channel_id:
            return
        ts = (body.get("message") or {}).get("ts")
        if not ts:
            return
        with contextlib.suppress(SlackApiError):
            await client.chat_delete(channel=channel_id, ts=ts)

    # One handler matches every ccslack_key:<...> action_id. Bolt's matcher
    # supports regex; we use a prefix predicate so each key only needs the
    # ``ccslack_key:<name>`` value in the layout above.
    import re as _re

    @app.action(_re.compile(r"^ccslack_key:.+$"))
    async def on_key(ack, body, _client) -> None:  # noqa: ANN001
        await ack()
        await _dispatch_key(body)


async def _dispatch_key(body: dict[str, Any]) -> None:
    """Resolve action_id → tmux key → send_keys to the bound window."""
    user_id = body.get("user", {}).get("id", "")
    channel_id = body.get("channel", {}).get("id", "")
    from .auth import is_authorized

    if not is_authorized(user_id, channel_id):
        return

    action_id = ""
    window_id = ""
    for action in body.get("actions", []) or []:
        aid = action.get("action_id", "")
        if aid.startswith("ccslack_key:"):
            action_id = aid
            window_id = action.get("value", "")
            break
    if not action_id or not window_id:
        return

    tmux_key = action_id.split(":", 1)[1]
    if not tmux_key:
        return

    try:
        await tmux_manager.send_keys(window_id, tmux_key, literal=False, enter=False)
    except OSError, RuntimeError:
        logger.exception("toolbar send_keys(%s, %s) failed", window_id, tmux_key)


def _extract_window_id(body: dict[str, Any], action_id: str) -> str:
    for action in body.get("actions", []) or []:
        if action.get("action_id") == action_id:
            return action.get("value", "")
    return ""


__all__ = [
    "build_toolbar_blocks",
    "open_toolbar",
    "register",
]
