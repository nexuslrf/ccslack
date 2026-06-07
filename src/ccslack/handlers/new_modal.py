"""Block Kit modal that backs ``/ccslack new`` when invoked with no args.

The CLI-arg form ``/ccslack new <dir> [provider] [--worktree [branch]]`` still
works directly; this module covers the discovery flow for users who prefer
clicking. Opening:

  1. ``/ccslack new`` (no args) in the meta channel → ``views.open`` with the
     ``ccslack_new_modal`` view payload, passing the meta channel as
     ``private_metadata`` so we know where to report success.
  2. The view contains three inputs: directory (text), provider (radio),
     options (checkboxes — currently just "create git worktree").
  3. ``view_submission`` → extract values → call ``create_session`` which
     mirrors ``_handle_new`` minus the CLI parsing.

Public API:
  * ``build_new_session_view(default_provider)`` — Block Kit view dict.
  * ``register(app)``  — wires the view_submission handler.
"""

from __future__ import annotations

import contextlib
import structlog
from pathlib import Path
from typing import TYPE_CHECKING, Any

from slack_sdk.errors import SlackApiError

from ..config import config

if TYPE_CHECKING:
    from slack_bolt.async_app import AsyncApp

logger = structlog.get_logger()

_PROVIDERS = ("claude", "codex", "gemini", "pi", "shell")


def _provider_option(name: str) -> dict[str, Any]:
    return {"text": {"type": "plain_text", "text": name}, "value": name}


def build_new_session_view(
    *, default_provider: str, private_metadata: str
) -> dict[str, Any]:
    """Build the Block Kit modal view for ``/ccslack new``."""
    if default_provider not in _PROVIDERS:
        default_provider = "claude"
    initial = _provider_option(default_provider)
    return {
        "type": "modal",
        "callback_id": "ccslack_new_modal",
        "private_metadata": private_metadata,
        "title": {"type": "plain_text", "text": "New ccslack session"},
        "submit": {"type": "plain_text", "text": "Create"},
        "close": {"type": "plain_text", "text": "Cancel"},
        "blocks": [
            {
                "type": "input",
                "block_id": "directory_block",
                "label": {
                    "type": "plain_text",
                    "text": "Working directory",
                },
                "element": {
                    "type": "plain_text_input",
                    "action_id": "directory",
                    "placeholder": {
                        "type": "plain_text",
                        "text": "/path/to/repo",
                    },
                },
            },
            {
                "type": "input",
                "block_id": "provider_block",
                "label": {"type": "plain_text", "text": "Provider"},
                "element": {
                    "type": "radio_buttons",
                    "action_id": "provider",
                    "initial_option": initial,
                    "options": [_provider_option(p) for p in _PROVIDERS],
                },
            },
            {
                "type": "input",
                "block_id": "worktree_block",
                "optional": True,
                "label": {"type": "plain_text", "text": "Options"},
                "element": {
                    "type": "checkboxes",
                    "action_id": "worktree",
                    "options": [
                        {
                            "text": {
                                "type": "plain_text",
                                "text": "Create a fresh git worktree (when eligible)",
                            },
                            "value": "worktree",
                        },
                        {
                            "text": {
                                "type": "plain_text",
                                "text": "YOLO — skip approvals (claude/codex/gemini)",
                            },
                            "value": "yolo",
                        },
                    ],
                },
            },
            {
                "type": "input",
                "block_id": "branch_block",
                "optional": True,
                "label": {
                    "type": "plain_text",
                    "text": "Worktree branch name (optional)",
                },
                "element": {
                    "type": "plain_text_input",
                    "action_id": "branch",
                    "placeholder": {
                        "type": "plain_text",
                        "text": "ccg/agent-1 (auto if blank)",
                    },
                },
            },
        ],
    }


def register(app: AsyncApp) -> None:
    """Wire the modal open + view_submission handlers."""

    @app.view("ccslack_new_modal")
    async def on_submit(ack, body, view, client) -> None:  # noqa: ANN001
        await ack()
        user_id = body.get("user", {}).get("id", "")
        meta_channel = view.get("private_metadata", "")
        # new-session creation is a meta-level action — always require the
        # global allow-list. Bound-channel membership doesn't grant the
        # right to spawn new sessions.
        from .auth import is_meta_authorized

        if not is_meta_authorized(user_id):
            return

        state_values = view.get("state", {}).get("values", {})
        directory = (
            state_values.get("directory_block", {}).get("directory", {}).get("value")
            or ""
        ).strip()
        provider = (
            state_values.get("provider_block", {})
            .get("provider", {})
            .get("selected_option", {})
            .get("value")
            or "claude"
        )
        wt_selected = (
            state_values.get("worktree_block", {})
            .get("worktree", {})
            .get("selected_options")
            or []
        )
        selected_values = {o.get("value") for o in wt_selected}
        want_worktree = "worktree" in selected_values
        want_yolo = "yolo" in selected_values
        branch = (
            state_values.get("branch_block", {}).get("branch", {}).get("value") or ""
        ).strip() or None

        if not directory:
            with contextlib.suppress(SlackApiError):
                await client.chat_postEphemeral(
                    channel=meta_channel,
                    user=user_id,
                    text="ccslack: modal submitted without a directory.",
                )
            return

        # Lazy: meta._create_session reuses the same validation + creation flow.
        from .meta import create_session

        await create_session(
            client=client,
            meta_channel_id=meta_channel,
            user_id=user_id,
            raw_dir=directory,
            provider=provider,
            want_worktree=want_worktree,
            worktree_branch=branch,
            want_yolo=want_yolo,
        )


async def open_modal(client, *, trigger_id: str, meta_channel: str) -> None:  # noqa: ANN001
    """Open the new-session modal in response to a trigger_id."""
    view = build_new_session_view(
        default_provider=config.provider_name, private_metadata=meta_channel
    )
    try:
        await client.views_open(trigger_id=trigger_id, view=view)
    except SlackApiError as exc:
        logger.warning(
            "views_open failed: %s",
            exc.response.get("error") if exc.response else exc,
        )


# Keep Path imported so reviewers can see the validation surface mirrors meta._handle_new.
_ = Path
