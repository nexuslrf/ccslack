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
from typing import TYPE_CHECKING, Any, Literal

from slack_sdk.errors import SlackApiError

from ..config import config
from ..providers import (
    get_provider_for_window,
    resolve_capabilities,
    resolve_launch_command,
)
from ..session import session_manager
from ..slack_client import BoltSlackClient
from ..thread_router import thread_router
from ..tmux_manager import tmux_manager
from ..window_state_store import window_store

if TYPE_CHECKING:
    from slack_bolt.async_app import AsyncApp

    from ..slack_client import SlackClient

logger = structlog.get_logger()

# How an agent should be relaunched when its window died.
RestoreMode = Literal["fresh", "continue", "resume"]


def _build_launch_args(window_id: str, mode: RestoreMode) -> str:
    """Build provider-correct CLI args for relaunching a dead window's agent.

    Uses ``provider.make_launch_args`` so each provider's resume syntax is
    honoured — Claude uses ``--continue`` / ``--resume <id>`` while Codex uses
    the ``resume --last`` / ``resume <id>`` subcommand form. Returns an empty
    string for ``fresh`` (or when the provider can't resume / no id is known).
    """
    if mode == "fresh":
        return ""
    view = session_manager.view_window(window_id)
    if view is None:
        return ""
    provider = get_provider_for_window(window_id, provider_name=view.provider_name)
    caps = provider.capabilities

    if mode == "resume":
        session_id = (view.session_id or "").strip()
        if session_id and caps.supports_resume:
            try:
                return provider.make_launch_args(resume_id=session_id)
            except ValueError:
                logger.warning(
                    "restore: invalid session id %r for %s; using continue",
                    session_id,
                    view.provider_name,
                )
        mode = "continue"  # fall through to continue when resume isn't possible

    if mode == "continue" and caps.supports_continue:
        return provider.make_launch_args(use_continue=True)
    return ""


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


async def restore_window(
    client: SlackClient,
    channel_id: str,
    window_id: str,
    *,
    mode: RestoreMode,
    announce: bool = True,
) -> str | None:
    """Respawn a dead window's agent and rebind the channel to the new window.

    Shared core for the recovery banner buttons, the ``/ccslack restore``
    command, and startup auto-recovery. Returns the new window_id on success,
    ``None`` on failure.

    ``mode`` selects provider-correct relaunch args via ``_build_launch_args``.
    ``announce`` posts a one-line confirmation into the channel when True.
    """
    extra_args = _build_launch_args(window_id, mode)
    success, message, new_window_id = await _respawn_window(
        window_id, extra_args=extra_args
    )
    if not success or not new_window_id:
        logger.warning(
            "restore: respawn failed for %s (%s): %s", window_id, mode, message
        )
        return None

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

    bolt_client = BoltSlackClient(client)
    # Lazy: status module pulls session_manager + slack helpers.
    from .status import ensure_status_message

    await ensure_status_message(
        bolt_client, channel_id, new_window_id, initial_state="idle"
    )

    if announce:
        label = {"fresh": "Fresh", "continue": "Continue", "resume": "Resume"}[mode]
        with contextlib.suppress(SlackApiError):
            await client.chat_postMessage(
                channel=channel_id,
                text=(
                    f":sparkles: {label} — new tmux window `{new_window_id}` "
                    f"(provider `{provider}`)."
                ),
            )
    return new_window_id


async def restore_dead_windows_on_start(client: SlackClient) -> None:
    """Auto-recover dead bound windows at startup per ``config.restore_on_start``.

    Called once from bootstrap after ``resolve_stale_ids``. For each bound
    channel whose tmux window no longer exists:

      * ``"off"``     — do nothing (polling will post a banner later).
      * ``"banner"``  — do nothing here; the polling loop posts the manual
                        recovery banner on its first tick.
      * ``"continue"``/``"resume"`` — respawn the agent automatically.

    Best-effort: failures are logged and the polling loop's banner remains the
    backstop for anything that couldn't be auto-restored.
    """
    mode = config.restore_on_start
    if mode in ("off", "banner"):
        return
    restore_mode: RestoreMode = "resume" if mode == "resume" else "continue"

    # Snapshot — restore_window mutates channel_bindings as it rebinds.
    bindings = list(thread_router.channel_bindings.items())
    restored = 0
    for channel_id, window_id in bindings:
        live = await tmux_manager.find_window_by_id(window_id)
        if live is not None:
            continue  # window survived (auto-detect / external) — leave it.
        view = session_manager.view_window(window_id)
        if view is None or not view.cwd:
            logger.info(
                "restore: skipping %s (channel %s) — no remembered cwd",
                window_id,
                channel_id,
            )
            continue
        new_window_id = await restore_window(
            client, channel_id, window_id, mode=restore_mode, announce=True
        )
        if new_window_id:
            restored += 1
            logger.info(
                "restore: auto-%s %s -> %s (channel %s)",
                restore_mode,
                window_id,
                new_window_id,
                channel_id,
            )
    if restored:
        logger.info("restore: auto-recovered %d session(s) at startup", restored)


def register(app: AsyncApp) -> None:
    """Wire the recovery-button action handlers."""

    @app.action("ccslack_recover_fresh")
    async def on_fresh(ack, body, client) -> None:  # noqa: ANN001
        await _handle_recover(ack, body, client, mode="fresh")

    @app.action("ccslack_recover_continue")
    async def on_continue(ack, body, client) -> None:  # noqa: ANN001
        await _handle_recover(ack, body, client, mode="continue")

    @app.action("ccslack_recover_resume")
    async def on_resume(ack, body, client) -> None:  # noqa: ANN001
        await _handle_recover(ack, body, client, mode="resume")

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
    mode: RestoreMode,
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

    new_window_id = await restore_window(
        client, channel_id, window_id, mode=mode, announce=True
    )
    if new_window_id is None:
        label = {"fresh": "Fresh", "continue": "Continue", "resume": "Resume"}[mode]
        with contextlib.suppress(SlackApiError):
            await client.chat_postEphemeral(
                channel=channel_id,
                user=user_id,
                text=f"ccslack {label}: respawn failed (check logs).",
            )
        return

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
    from .messaging_pipeline.turn_threads import clear_channel
    from .polling.coordinator import forget_window

    forget_window(window_id)
    clear_channel(channel_id)
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


__all__ = [
    "RestoreMode",
    "post_recovery_banner",
    "register",
    "restore_dead_windows_on_start",
    "restore_window",
]
