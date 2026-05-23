"""Dead-window recovery banner.

Posted by the polling coordinator the first time it detects that a bound tmux
window has vanished. The banner offers four options as Block Kit buttons:

  * **Fresh**    — spawn a new tmux window with the same provider + cwd, rebind
                  the channel, drop the old session.
  * **Continue** — same as Fresh but appends the provider's ``--continue`` flag.
  * **Resume**   — same as Fresh but appends ``--resume <session_id>``. Stub:
                  the resume picker modal lands later.
  * **Archive**  — archive the Slack channel and forget the window.

Only one banner is posted per dead window — ``WindowState.status_state == "dead"``
gates the post. Clicking any action edits the banner in place (or deletes it)
and writes the new ``WindowState`` / ``channel_bindings``.
"""

from __future__ import annotations

import contextlib
import structlog
from typing import TYPE_CHECKING, Any

from slack_sdk.errors import SlackApiError

from ..config import config
from ..providers import resolve_capabilities, resolve_launch_command
from ..session import session_manager
from ..slack_client import BoltSlackClient
from ..thread_router import thread_router
from ..tmux_manager import tmux_manager
from ..window_state_store import window_store

if TYPE_CHECKING:
    from slack_bolt.async_app import AsyncApp

    from ..slack_client import SlackClient

logger = structlog.get_logger()


def _build_banner_blocks(window_id: str) -> tuple[list[dict[str, Any]], str]:
    """Build the recovery banner blocks for a dead window."""
    view = session_manager.view_window(window_id)
    provider = (view.provider_name if view else "") or "?"
    cwd = (view.cwd if view else "") or "?"
    session_id = (view.session_id if view else "") or ""
    caps = resolve_capabilities(provider) if view else None

    fallback = (
        f"Dead session — pick a recovery for {provider} in {cwd} (window {window_id})."
    )
    elements: list[dict[str, Any]] = [
        {
            "type": "button",
            "action_id": "ccslack_recover_fresh",
            "style": "primary",
            "text": {"type": "plain_text", "text": ":sparkles: Fresh"},
            "value": window_id,
        }
    ]
    if caps and caps.supports_continue:
        elements.append(
            {
                "type": "button",
                "action_id": "ccslack_recover_continue",
                "text": {
                    "type": "plain_text",
                    "text": ":arrows_counterclockwise: Continue",
                },
                "value": window_id,
            }
        )
    if caps and caps.supports_resume and session_id:
        elements.append(
            {
                "type": "button",
                "action_id": "ccslack_recover_resume",
                "text": {"type": "plain_text", "text": ":rewind: Resume"},
                "value": window_id,
            }
        )
    elements.append(
        {
            "type": "button",
            "action_id": "ccslack_recover_archive",
            "style": "danger",
            "text": {"type": "plain_text", "text": ":wastebasket: Archive"},
            "value": window_id,
        }
    )

    blocks: list[dict[str, Any]] = [
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": (
                    ":x: *Session died.*\n"
                    f"provider `{provider}` · cwd `{cwd}` · tmux window `{window_id}`\n"
                    "Choose a recovery:"
                ),
            },
        },
        {
            "type": "actions",
            "block_id": f"ccslack_recover_actions:{window_id}",
            "elements": elements,
        },
    ]
    return blocks, fallback


async def post_recovery_banner(
    client: SlackClient, channel_id: str, window_id: str
) -> str | None:
    """Post the dead-window banner. Returns the new message ts (or None)."""
    blocks, fallback = _build_banner_blocks(window_id)
    try:
        result = await client.chat_postMessage(
            channel=channel_id, text=fallback, blocks=blocks
        )
        return result.get("ts") if hasattr(result, "get") else result["ts"]
    except SlackApiError as exc:
        logger.warning(
            "recovery banner post failed: %s",
            exc.response.get("error") if exc.response else exc,
        )
        return None


async def _respawn_window(
    window_id: str, *, extra_args: str = ""
) -> tuple[bool, str, str]:
    """Spawn a fresh tmux window using the same provider + cwd as ``window_id``.

    Returns ``(success, message, new_window_id)``. On success the caller is
    responsible for rebinding the channel.
    """
    view = session_manager.view_window(window_id)
    if view is None or not view.cwd:
        return False, "no remembered cwd for this window", ""
    provider = view.provider_name or config.provider_name
    launch_command = None if provider == "shell" else resolve_launch_command(provider)
    success, message, _, new_window_id = await tmux_manager.create_window(
        work_dir=view.cwd,
        window_name=thread_router.get_display_name(window_id),
        start_agent=launch_command is not None,
        agent_args=extra_args,
        launch_command=launch_command,
    )
    return success, message, new_window_id


def register(app: AsyncApp) -> None:
    """Wire the recovery-button action handlers."""

    @app.action("ccslack_recover_fresh")
    async def on_fresh(ack, body, client) -> None:  # noqa: ANN001
        await _handle_recover(ack, body, client, extra_args="", label="Fresh")

    @app.action("ccslack_recover_continue")
    async def on_continue(ack, body, client) -> None:  # noqa: ANN001
        await _handle_recover(
            ack, body, client, extra_args="--continue", label="Continue"
        )

    @app.action("ccslack_recover_resume")
    async def on_resume(ack, body, client) -> None:  # noqa: ANN001
        view_window_id = _extract_window_id(body, "ccslack_recover_resume")
        view = session_manager.view_window(view_window_id) if view_window_id else None
        session_id = (view.session_id if view else "") or ""
        extra = f"--resume {session_id}" if session_id else ""
        await _handle_recover(ack, body, client, extra_args=extra, label="Resume")

    @app.action("ccslack_recover_archive")
    async def on_archive(ack, body, client) -> None:  # noqa: ANN001
        await ack()
        user_id = body.get("user", {}).get("id", "")
        channel_id = body.get("channel", {}).get("id", "")
        window_id = _extract_window_id(body, "ccslack_recover_archive")
        from .auth import is_authorized

        if not is_authorized(user_id, channel_id) or not channel_id or not window_id:
            return
        await _archive(client, channel_id, window_id, body=body)


async def _handle_recover(
    ack,  # noqa: ANN001
    body: dict[str, Any],
    client,  # noqa: ANN001 — Bolt-provided AsyncWebClient
    *,
    extra_args: str,
    label: str,
) -> None:
    """Common implementation for Fresh / Continue / Resume buttons."""
    await ack()
    user_id = body.get("user", {}).get("id", "")
    channel_id = body.get("channel", {}).get("id", "")
    window_id = _extract_window_id(
        body, body.get("actions", [{}])[0].get("action_id", "")
    )
    from .auth import is_authorized

    if not is_authorized(user_id, channel_id) or not channel_id or not window_id:
        return

    success, message, new_window_id = await _respawn_window(
        window_id, extra_args=extra_args
    )
    if not success or not new_window_id:
        with contextlib.suppress(SlackApiError):
            await client.chat_postEphemeral(
                channel=channel_id,
                user=user_id,
                text=f"ccslack {label}: respawn failed — {message}",
            )
        return

    # Rebind channel to new window, drop the old state.
    old_state = window_store.window_states.get(window_id)
    provider = old_state.provider_name if old_state else config.provider_name
    cwd = (old_state.cwd if old_state else "") or None

    thread_router.unbind_channel(channel_id)
    window_store.remove_window(window_id)
    thread_router.bind_channel(
        channel_id,
        new_window_id,
        window_name=thread_router.get_display_name(new_window_id),
    )
    session_manager.set_window_provider(new_window_id, provider, cwd=cwd)
    session_manager.set_window_origin(new_window_id, "ccslack_created")

    # Repost a fresh status message for the new window.
    bolt_client = BoltSlackClient(client)
    # Lazy: status module pulls session_manager + slack helpers.
    from .status import ensure_status_message

    await ensure_status_message(
        bolt_client, channel_id, new_window_id, initial_state="idle"
    )

    with contextlib.suppress(SlackApiError):
        await client.chat_postMessage(
            channel=channel_id,
            text=(
                f":sparkles: {label} — new tmux window `{new_window_id}` "
                f"(provider `{provider}`)."
            ),
        )

    # Hide the recovery banner so it can't be re-clicked.
    message_ts = (body.get("message") or {}).get("ts")
    if message_ts:
        with contextlib.suppress(SlackApiError):
            await client.chat_delete(channel=channel_id, ts=message_ts)


async def _archive(
    client,  # noqa: ANN001
    channel_id: str,
    window_id: str,
    *,
    body: dict[str, Any],
) -> None:
    """Archive the Slack channel and forget the window state."""
    thread_router.unbind_channel(channel_id)
    window_store.remove_window(window_id)
    # Lazy: polling helper for cleanup.
    from .polling.coordinator import forget_window

    forget_window(window_id)
    message_ts = (body.get("message") or {}).get("ts")
    if message_ts:
        with contextlib.suppress(SlackApiError):
            await client.chat_delete(channel=channel_id, ts=message_ts)
    with contextlib.suppress(SlackApiError):
        await client.conversations_archive(channel=channel_id)


def _extract_window_id(body: dict[str, Any], action_id: str) -> str:
    """Pull the ``value`` for a given ``action_id`` out of the Bolt action body."""
    for action in body.get("actions", []) or []:
        if action.get("action_id") == action_id:
            return action.get("value", "")
    return ""


__all__ = ["post_recovery_banner", "register"]
