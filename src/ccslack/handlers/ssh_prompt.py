"""Slack UI for SSH interactive-auth prompts (Duo/2FA) bridged from a tunnel.

The router posts an auth prompt to the meta channel with one button per parsed
option plus a "Passcode…" button (a modal for free text). A click writes the
answer back to the waiting ``ssh`` process via ``ssh_auth.respond(host, …)``.

Gated by ``ALLOWED_USERS`` — answering an SSH 2FA challenge is a privileged
action. Only wired in the router process; harmless elsewhere (no prompts fire).
"""

from __future__ import annotations

import contextlib
import structlog
from typing import TYPE_CHECKING, Any

from slack_sdk.errors import SlackApiError

from ..config import config

if TYPE_CHECKING:
    from slack_bolt.async_app import AsyncApp

logger = structlog.get_logger()

# Slack button labels truncate; keep option text short.
_OPT_LABEL_MAX = 70
# Cap how many option buttons we render (Slack actions block ≤ 25 elements).
_MAX_OPTIONS = 8


def build_prompt_blocks(
    host: str, text: str, options: list[tuple[str, str]]
) -> list[dict[str, Any]]:
    """Block Kit for an auth prompt: the text + an option/passcode toolbar."""
    elements: list[dict[str, Any]] = []
    for number, label in options[:_MAX_OPTIONS]:
        btn_label = f"{number} · {label}"
        if len(btn_label) > _OPT_LABEL_MAX:
            btn_label = btn_label[: _OPT_LABEL_MAX - 1] + "…"
        elements.append(
            {
                "type": "button",
                "action_id": f"ccslack_ssh_opt:{number}",
                "text": {"type": "plain_text", "text": btn_label},
                "value": f"{host}|{number}",
            }
        )
    elements.append(
        {
            "type": "button",
            "action_id": "ccslack_ssh_pass",
            "style": "primary",
            "text": {"type": "plain_text", "text": ":closed_lock_with_key: Passcode…"},
            "value": host,
        }
    )
    return [
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f":lock: *SSH auth needed* — host `{host}`\n```{text}```",
            },
        },
        {"type": "actions", "block_id": f"ccslack_ssh_actions:{host}", "elements": elements},
    ]


async def post_prompt(
    client,  # noqa: ANN001 — BoltSlackClient / AsyncWebClient
    host: str,
    text: str,
    options: list[tuple[str, str]],
) -> None:
    """Post an auth-prompt toolbar to the meta channel (best-effort)."""
    with contextlib.suppress(SlackApiError):
        await client.chat_postMessage(
            channel=config.meta_channel_id,
            text=f"SSH auth needed for host {host}",
            blocks=build_prompt_blocks(host, text, options),
        )


def _passcode_view(host: str, channel_id: str, message_ts: str) -> dict[str, Any]:
    """Modal for entering an SSH passcode; private_metadata carries delivery context."""
    return {
        "type": "modal",
        "callback_id": "ccslack_ssh_pass_modal",
        "private_metadata": f"{host}|{channel_id}|{message_ts}",
        "title": {"type": "plain_text", "text": "SSH passcode"},
        "submit": {"type": "plain_text", "text": "Send"},
        "close": {"type": "plain_text", "text": "Cancel"},
        "blocks": [
            {
                "type": "input",
                "block_id": "passcode_block",
                "label": {"type": "plain_text", "text": f"Passcode for `{host}`"},
                "element": {"type": "plain_text_input", "action_id": "passcode"},
            }
        ],
    }


async def _deliver(client, channel_id: str, message_ts: str, host: str, answer: str, shown: str) -> None:  # noqa: ANN001
    """Write *answer* to the tunnel and collapse the prompt message."""
    from .. import ssh_auth

    ok = await ssh_auth.respond(host, answer)
    note = (
        f":unlock: Sent `{shown}` to host `{host}`."
        if ok
        else f":warning: Couldn't deliver to host `{host}` (tunnel not waiting?)."
    )
    if message_ts and channel_id:
        with contextlib.suppress(SlackApiError):
            await client.chat_update(channel=channel_id, ts=message_ts, text=note, blocks=[])


def _authorized(body: dict[str, Any]) -> bool:
    from .auth import is_meta_authorized

    return is_meta_authorized(body.get("user", {}).get("id", ""))


def _action_value(body: dict[str, Any], prefix: str) -> str:
    for action in body.get("actions", []) or []:
        if str(action.get("action_id", "")).startswith(prefix):
            return action.get("value", "")
    return ""


async def _handle_option(body: dict[str, Any], client) -> None:  # noqa: ANN001
    if not _authorized(body):
        return
    host, _, number = _action_value(body, "ccslack_ssh_opt:").partition("|")
    if not host or not number:
        return
    channel_id = body.get("channel", {}).get("id", "")
    message_ts = (body.get("message") or {}).get("ts", "")
    await _deliver(client, channel_id, message_ts, host, number, f"option {number}")


async def _handle_passcode_open(body: dict[str, Any], client) -> None:  # noqa: ANN001
    if not _authorized(body):
        return
    host = _action_value(body, "ccslack_ssh_pass")
    trigger_id = body.get("trigger_id", "")
    if not host or not trigger_id:
        return
    channel_id = body.get("channel", {}).get("id", "")
    message_ts = (body.get("message") or {}).get("ts", "")
    with contextlib.suppress(SlackApiError):
        await client.views_open(
            trigger_id=trigger_id, view=_passcode_view(host, channel_id, message_ts)
        )


async def _handle_passcode_submit(body: dict[str, Any], view: dict[str, Any], client) -> None:  # noqa: ANN001
    if not _authorized(body):
        return
    host, _, rest = view.get("private_metadata", "").partition("|")
    channel_id, _, message_ts = rest.partition("|")
    passcode = (
        view.get("state", {})
        .get("values", {})
        .get("passcode_block", {})
        .get("passcode", {})
        .get("value")
        or ""
    ).strip()
    if not host or not passcode:
        return
    await _deliver(client, channel_id, message_ts, host, passcode, "passcode")


def register(app: AsyncApp) -> None:
    """Wire the SSH-auth option buttons + passcode modal."""
    import re as _re

    @app.action(_re.compile(r"^ccslack_ssh_opt:.+$"))
    async def on_option(ack, body, client) -> None:  # noqa: ANN001
        await ack()
        await _handle_option(body, client)

    @app.action("ccslack_ssh_pass")
    async def on_passcode(ack, body, client) -> None:  # noqa: ANN001
        await ack()
        await _handle_passcode_open(body, client)

    @app.view("ccslack_ssh_pass_modal")
    async def on_passcode_submit(ack, body, view, client) -> None:  # noqa: ANN001
        await ack()
        await _handle_passcode_submit(body, view, client)


__all__ = ["build_prompt_blocks", "post_prompt", "register"]
