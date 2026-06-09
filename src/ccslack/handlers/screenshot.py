"""On-demand terminal screenshot uploaded to Slack as PNG.

Triggered by:

  * ``ccslack_screenshot`` Block Kit action button on the pinned status
    message (one per channel).
  * (future) ``/ccslack screenshot`` slash subcommand.

Captures the tmux pane's scrollback (default ``CCSLACK_SCREENSHOT_HISTORY``
lines) with ANSI colour preserved, renders it to a PNG via
``screenshot.text_to_image``, and uploads via ``files_upload_v2`` into the
calling channel.
"""

from __future__ import annotations

import contextlib
import io
import structlog
from typing import TYPE_CHECKING

from slack_sdk.errors import SlackApiError

from ..screenshot import text_to_image
from ..slack_client import BoltSlackClient
from ..thread_router import thread_router
from ..tmux_manager import tmux_manager

if TYPE_CHECKING:
    from slack_bolt.async_app import AsyncApp

logger = structlog.get_logger()


async def capture_window(window_id: str) -> bytes | None:
    """Capture the visible viewport of ``window_id`` and return PNG bytes.

    Matches ccgram: ``/screenshot`` captures *only the viewport* so successive
    screenshots stay the same size and focus on the most-recent operations.
    The ``CCSLACK_SCREENSHOT_HISTORY`` env var still drives prompt-marker
    context elsewhere but is intentionally not used here — using it caused
    the screenshot to grow linearly as the tmux scrollback filled up.
    """
    text = await tmux_manager.capture_pane(window_id, with_ansi=True)
    if not text:
        return None
    try:
        return await text_to_image(text, with_ansi=True)
    except OSError, ValueError:
        logger.exception("text_to_image failed for %s", window_id)
        return None


async def upload_screenshot(
    client,  # noqa: ANN001 — Bolt-provided AsyncWebClient
    *,
    channel_id: str,
    window_id: str,
    user_id: str | None = None,
) -> bool:
    """Capture + upload a screenshot for ``window_id``. Returns success."""
    png = await capture_window(window_id)
    if png is None:
        if user_id is not None:
            with contextlib.suppress(SlackApiError):
                await client.chat_postEphemeral(
                    channel=channel_id,
                    user=user_id,
                    text=f"ccslack: couldn't capture pane for `{window_id}`.",
                )
        return False

    bolt_client = BoltSlackClient(client)
    display = thread_router.get_display_name(window_id)
    try:
        await bolt_client.files_upload_v2(
            channel=channel_id,
            file=io.BytesIO(png),
            filename=f"{display or window_id}.png",
            title=f"Screenshot · {display or window_id}",
        )
    except SlackApiError as exc:
        logger.warning(
            "files_upload_v2 failed: %s",
            exc.response.get("error") if exc.response else exc,
        )
        if user_id is not None:
            with contextlib.suppress(SlackApiError):
                await client.chat_postEphemeral(
                    channel=channel_id,
                    user=user_id,
                    text="ccslack: file upload failed — check `files:write` scope.",
                )
        return False
    return True


def register(app: AsyncApp) -> None:
    """Wire the ``ccslack_screenshot`` action button + a ``/ccslack screenshot`` shortcut."""

    @app.action("ccslack_screenshot")
    async def on_screenshot_click(ack, body, client) -> None:  # noqa: ANN001
        await ack()
        user_id = body.get("user", {}).get("id", "")
        channel_id = body.get("channel", {}).get("id", "")
        # Lazy: keep auth wire local to the handler call site.
        from .auth import is_authorized

        if not is_authorized(user_id, channel_id):
            return
        button_value = ""
        for action in body.get("actions", []) or []:
            if action.get("action_id") == "ccslack_screenshot":
                button_value = action.get("value", "")
                break
        # Prefer the channel's live binding — a restore may have rebound this
        # channel to a new window since this button was posted.
        window_id = thread_router.effective_window_id(channel_id, button_value)
        if not window_id or not channel_id:
            return
        await upload_screenshot(
            client, channel_id=channel_id, window_id=window_id, user_id=user_id
        )


__all__ = ["capture_window", "register", "upload_screenshot"]
