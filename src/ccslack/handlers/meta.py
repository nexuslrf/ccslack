"""Meta-channel handlers — slash command dispatcher.

Subcommands (walking-skeleton scope):

  * ``<cmd> help``                 — usage
  * ``<cmd> new <dir> [provider]`` — create a private session channel,
    invite the user, spawn a tmux window with the agent CLI, bind channel↔window.
  * ``<cmd> list``                 — list active sessions.

The actual slash command name is read from ``config.slash_command``
(``CCSLACK_SLASH_COMMAND`` env, default ``/ccslack``) so workspaces can avoid
collisions with other apps that registered the same name.

Only allowed in the configured meta channel. Replies in other channels go via
``chat_postEphemeral`` so the bot doesn't litter unrelated channels.
"""

from __future__ import annotations

import contextlib
import re
import shlex
import structlog
from pathlib import Path
from typing import TYPE_CHECKING, Any

from slack_sdk.errors import SlackApiError

from ..config import config
from ..providers import has_yolo_mode, resolve_launch_command
from ..session import session_manager
from ..slack_client import BoltSlackClient
from ..thread_router import thread_router
from ..tmux_manager import tmux_manager
from ..window_resolver import is_window_id
from ..window_state_store import window_store
from .status import clear_status_message, ensure_status_message

if TYPE_CHECKING:
    from slack_bolt.async_app import AsyncApp

logger = structlog.get_logger()

_SUPPORTED_PROVIDERS = ("claude", "codex", "gemini", "pi", "shell")
_CHANNEL_NAME_SAFE = re.compile(r"[^a-z0-9-]+")

# Slack user references in slash-command text: ``<@U123|name>`` / ``<@U123>``
# (when "escape users" is on) or a bare ``U…`` / ``W…`` id.
_USER_MENTION_RE = re.compile(r"^<@([UW][A-Z0-9]+)(?:\|[^>]*)?>$")
_BARE_USER_RE = re.compile(r"^[UW][A-Z0-9]{6,}$")


def _parse_user_ids(args: list[str]) -> list[str]:
    """Extract Slack user ids from slash-command args (mentions or bare ids)."""
    ids: list[str] = []
    for token in args:
        match = _USER_MENTION_RE.match(token)
        if match:
            ids.append(match.group(1))
        elif _BARE_USER_RE.match(token):
            ids.append(token)
    # De-dup, preserve order.
    seen: set[str] = set()
    return [uid for uid in ids if not (uid in seen or seen.add(uid))]


def _sanitize_channel_name(raw: str) -> str:
    """Turn a string into a Slack-legal private channel slug.

    Slack rules: lowercase, alphanumeric + hyphen + underscore, ≤80 chars,
    cannot start/end with hyphen.
    """
    slug = raw.lower().replace("/", "-").replace(" ", "-")
    slug = _CHANNEL_NAME_SAFE.sub("-", slug)
    slug = slug.strip("-_") or "session"
    return slug[:60]


def _channel_name_for(cwd: Path) -> str:
    """Default channel name from a session's cwd.

    Prefixed with ``config.channel_prefix`` (``CCSLACK_CHANNEL_PREFIX``,
    default ``ccslack``). An empty prefix yields just the cwd slug.
    """
    slug = _sanitize_channel_name(cwd.name)
    prefix = _sanitize_channel_name(config.channel_prefix) if config.channel_prefix else ""
    return f"{prefix}-{slug}" if prefix else slug


# Slack channel names: ≤80 chars. How many distinct names to probe before
# giving up when each is already taken (live OR archived channels both reserve
# the name, so a same-cwd session needs to walk past prior channels).
_CHANNEL_NAME_MAX_LEN = 80
_CHANNEL_NAME_MAX_TRIES = 30

# Slack errors that mean "the bot isn't permitted to do this channel op" — used
# to fall back to manual instructions rather than a bare error (office mode,
# where channel-management scopes may be withheld).
_CHANNEL_DENIED_ERRORS: frozenset[str] = frozenset(
    {
        "missing_scope",
        "not_allowed_token_type",
        "restricted_action",
        "permission_denied",
        "team_access_not_granted",
        "method_not_supported_for_channel_type",
        "user_is_restricted",
    }
)


def _suffixed_channel_name(base: str, suffix: str | int) -> str:
    """``base`` with ``-<suffix>`` appended, trimmed to Slack's length cap."""
    tail = f"-{suffix}"
    trimmed = base[: _CHANNEL_NAME_MAX_LEN - len(tail)].rstrip("-_")
    return f"{trimmed}{tail}"


def _channel_name_candidates(base: str, window_id: str) -> list[str]:
    """Ordered, de-duplicated channel-name candidates for a new session.

    Two sessions on the same cwd produce the same base name, and archived
    channels keep their names reserved, so a single name often isn't enough.
    Tries the bare name first, then the window id, then ``-2``, ``-3`` … so a
    free name is found even when several prior channels exist for the cwd.
    """
    candidates = [base[:_CHANNEL_NAME_MAX_LEN]]
    wid = window_id.lstrip("@")
    if wid:
        candidates.append(_suffixed_channel_name(base, wid))
    candidates.extend(
        _suffixed_channel_name(base, i) for i in range(2, _CHANNEL_NAME_MAX_TRIES + 1)
    )
    seen: set[str] = set()
    ordered: list[str] = []
    for name in candidates:
        if name and name not in seen:
            seen.add(name)
            ordered.append(name)
    return ordered


async def _create_unique_channel(
    bolt_client,  # noqa: ANN001 — BoltSlackClient
    base: str,
    window_id: str,
) -> tuple[str | None, str]:
    """Create a session channel, walking past ``name_taken`` collisions.

    Private by default; public when ``CCSLACK_PUBLIC_CHANNELS`` is set (office
    mode). Returns ``(channel_id, "")`` on success or ``(None, error)`` when
    every candidate is taken or Slack returns a non-``name_taken`` error.
    """
    is_private = not config.public_channels
    last_error = "name_taken"
    for name in _channel_name_candidates(base, window_id):
        try:
            result = await bolt_client.conversations_create(
                name=name, is_private=is_private
            )
            return result["channel"]["id"], ""
        except SlackApiError as exc:
            last_error = (exc.response.get("error") if exc.response else "") or str(exc)
            if last_error != "name_taken":
                logger.exception("conversations_create failed for %s", name)
                return None, last_error
            logger.info("channel name %s taken; trying next candidate", name)
    return None, last_error


async def _post_ephemeral(client_method, **kwargs: Any) -> None:
    """Best-effort ephemeral reply; ignored if Slack rejects the call."""
    try:
        await client_method(**kwargs)
    except SlackApiError as exc:
        logger.warning("ephemeral reply failed: %s", exc.response.get("error"))


def register(app: AsyncApp) -> None:
    """Wire the configured slash command (``config.slash_command``)."""
    register_dashboard_actions(app)
    register_yolo_actions(app)
    register_join_actions(app)

    slash = config.slash_command

    @app.command(slash)
    async def on_slash_command(ack, body, client) -> None:  # noqa: ANN001
        await ack()
        user_id = body.get("user_id", "")
        channel_id = body.get("channel_id", "")
        raw_text = (body.get("text") or "").strip()

        # Auth: bound session-channel members are trusted by virtue of
        # membership (the bot itself invited everyone there). Anywhere else
        # — meta channel, unrelated channels, DMs — requires ALLOWED_USERS.
        from .auth import is_authorized

        if not is_authorized(user_id, channel_id):
            await _post_ephemeral(
                client.chat_postEphemeral,
                channel=channel_id,
                user=user_id,
                text="ccslack: you're not in `ALLOWED_USERS`.",
            )
            return

        # Parse subcommand first so we can apply meta-channel restriction
        # only to subcommands that need it.
        parts = shlex.split(raw_text) if raw_text else []
        sub = parts[0].lower() if parts else "help"
        args = parts[1:]

        # `kill`, `mute`, `history` are allowed from any bound session channel.
        # All other subcommands are meta-channel only.
        meta_only = sub not in (
            "kill",
            "mute",
            "history",
            "resume",
            "restore",
            "panes",
            "send",
            "rename",
            "toolcalls",
            "thread",
            "yolo",
            "chat",
            "here",
            "adduser",
            "removeuser",
            "users",
            "purge",
            "autopurge",
            "help",
            "?",
            "-h",
            "--help",
        )
        if meta_only and channel_id != config.meta_channel_id:
            await _post_ephemeral(
                client.chat_postEphemeral,
                channel=channel_id,
                user=user_id,
                text=(
                    f"ccslack: `{slash} {sub}` only works in the meta channel "
                    f"(<#{config.meta_channel_id}>)."
                ),
            )
            return

        if sub in ("help", "?", "-h", "--help"):
            await _post_ephemeral(
                client.chat_postEphemeral,
                channel=channel_id,
                user=user_id,
                text=_help_text(),
            )
            return

        if sub == "new":
            if not args:
                # No-arg form opens the Block Kit modal — needs the slash
                # command's trigger_id (valid for 3 seconds).
                trigger_id = body.get("trigger_id", "")
                if not trigger_id:
                    await _post_ephemeral(
                        client.chat_postEphemeral,
                        channel=channel_id,
                        user=user_id,
                        text="ccslack: missing trigger_id; can't open modal.",
                    )
                    return
                # Lazy: modal module pulls Block Kit builders.
                from .new_modal import open_modal

                await open_modal(client, trigger_id=trigger_id, meta_channel=channel_id)
                return
            await _handle_new(client, channel_id, user_id, args)
            return

        if sub == "list":
            await _handle_list(client, channel_id, user_id)
            return

        if sub == "kill":
            await _handle_kill(client, channel_id, user_id, args)
            return

        if sub == "chat":
            await _handle_chat(client, channel_id, user_id, args)
            return

        if sub == "here":
            await _handle_here(client, channel_id, user_id, args)
            return

        if sub in ("adduser", "removeuser"):
            await _handle_grant(client, channel_id, user_id, args, grant=sub == "adduser")
            return

        if sub == "users":
            await _handle_users(client, channel_id, user_id)
            return

        if sub == "purge":
            await _handle_purge(client, channel_id, user_id, args)
            return

        if sub == "autopurge":
            await _handle_autopurge(client, channel_id, user_id, args)
            return

        if sub == "mute":
            await _handle_mute(client, channel_id, user_id, args)
            return

        if sub == "sessions":
            await _handle_sessions(client, channel_id, user_id)
            return

        if sub == "history":
            # Lazy: history pulls transcript reader.
            from .history import handle_history

            raw_limit = args[0] if args else ""
            await handle_history(client, channel_id, user_id, raw_limit)
            return

        if sub == "resume":
            # Lazy: resume pulls session resolver + tmux.
            from .resume import handle_resume

            await handle_resume(client, channel_id, user_id)
            return

        if sub == "panes":
            # Lazy: panes pulls tmux pane list.
            from .panes import handle_panes

            await handle_panes(client, channel_id, user_id)
            return

        if sub == "restore":
            await _handle_restore(client, channel_id, user_id, args)
            return

        if sub == "send":
            # Lazy: send pulls security predicates + uploader.
            from .send import handle_send

            raw_path = args[0] if args else ""
            await handle_send(client, channel_id, user_id, raw_path)
            return

        if sub == "rename":
            await _handle_rename(client, channel_id, user_id, args)
            return

        if sub == "toolcalls":
            await _handle_toolcalls(client, channel_id, user_id, args)
            return

        if sub == "thread":
            await _handle_thread(client, channel_id, user_id, args)
            return

        if sub == "yolo":
            await _handle_yolo(client, channel_id, user_id, args)
            return

        await _post_ephemeral(
            client.chat_postEphemeral,
            channel=channel_id,
            user=user_id,
            text=f"ccslack: unknown subcommand `{sub}`. Try `{slash} help`.",
        )


def _help_text() -> str:
    slash = config.slash_command
    return (
        "*ccslack commands*\n"
        f"• `{slash} new <directory> [provider] [--worktree [branch]] [--yolo] "
        "[--host <name>]` — start a new session.\n"
        "    provider ∈ {claude, codex, gemini, pi, shell}; default: "
        f"`{config.provider_name}`.\n"
        "    `--yolo` launches claude/codex/gemini with approvals skipped "
        "(dangerous).\n"
        "    `--host <name>` runs the session on a specific fleet host "
        "(multi-host router).\n"
        f"• `{slash} list` — quick list of active sessions.\n"
        f"• `{slash} sessions` — interactive dashboard with per-session kill.\n"
        f"• `{slash} history [N]` — last N transcript messages in this channel.\n"
        f"• `{slash} resume` — pick a past Claude session in this channel's cwd.\n"
        f"• `{slash} restore [continue|resume|fresh]` — respawn a dead session "
        "(after reboot / tmux restart).\n"
        f"• `{slash} panes` — list all tmux panes for this session.\n"
        f"• `{slash} rename <new-name>` — rename this session's Slack channel.\n"
        f"• `{slash} yolo [on|off]` — Ctrl-C the running agent and restart it "
        "with approvals/sandbox skipped (`on`, default) or required again "
        "(`off`) — claude/codex/gemini only.\n"
        f"• `{slash} send [path|glob|substring]` — upload file(s) from the "
        "session's cwd (e.g. `send docs/arch.png`, `send *.png`, `send arch`). "
        "With no argument, opens an interactive file browser.\n"
        f"• `{slash} toolcalls [shown|hidden|default]` — show/hide tool_use & "
        "tool_result for this channel.\n"
        f"• `{slash} thread [on|off|default]` — group tool chains under a "
        "thread parent (vs flat).\n"
        f"• `{slash} kill` — kill the session for THIS channel.\n"
        f"• `{slash} kill <#channel|@window>` — kill a specific session "
        "(meta channel only).\n"
        f"• `{slash} kill --all --confirm` — kill every session "
        "(meta channel only).\n"
        f"• `{slash} mute [all|errors|off]` — change/cycle notify mode for "
        "the current channel.\n"
        f"• `{slash} chat [topic]` — start a human-only thread; replies in it "
        "are not sent to the agent.\n"
        f"• `{slash} here <dir> [provider]` — bind THIS channel to a fresh "
        "session (for channels you created + invited the bot to).\n"
        f"• `{slash} adduser @user` / `removeuser @user` / `users` — manage "
        "who may drive this session (public mode; `ALLOWED_USERS` only).\n"
        f"• `{slash} purge [N|all|since <dur>]` — delete ccslack's own output "
        "in this channel (not your messages or chat threads).\n"
        f"• `{slash} autopurge [off|Xh]` — auto-delete output older than X "
        "hours (default off).\n"
        f"• `{slash} help` — this message."
    )


async def _handle_new(
    client,
    channel_id: str,
    user_id: str,
    args: list[str],  # noqa: ANN001
) -> None:
    """Implements ``/ccslack new <dir> [provider]``."""
    if not args:
        await _post_ephemeral(
            client.chat_postEphemeral,
            channel=channel_id,
            user=user_id,
            text="ccslack: usage `/ccslack new <directory> [provider]`",
        )
        return

    # Parse optional --worktree [branch-name], --yolo, and --host <name> flags.
    want_worktree = False
    want_yolo = False
    worktree_branch: str | None = None
    want_host: str | None = None
    cleaned: list[str] = []
    i = 0
    while i < len(args):
        a = args[i]
        if a == "--worktree":
            want_worktree = True
            nxt = args[i + 1] if i + 1 < len(args) else ""
            if nxt and not nxt.startswith("-"):
                worktree_branch = nxt
                i += 2
                continue
        elif a in ("--yolo", "--dangerous"):
            want_yolo = True
        elif a == "--host":
            nxt = args[i + 1] if i + 1 < len(args) else ""
            if nxt and not nxt.startswith("-"):
                want_host = nxt
                i += 2
                continue
        elif a.startswith("--host="):
            want_host = a[len("--host=") :]
        else:
            cleaned.append(a)
        i += 1
    args = cleaned

    # Multi-host: the router forwards `--host <name>` to that worker, so by the
    # time we run, a set --host should equal this host. If it names another host
    # the router couldn't route it there (unknown / disconnected) — report it
    # with the available hosts rather than silently creating on the wrong box.
    from .. import fleet_state

    if want_host is not None and want_host != config.host_name:
        await _post_ephemeral(
            client.chat_postEphemeral,
            channel=channel_id,
            user=user_id,
            text=(
                f"ccslack: host `{want_host}` isn't available. "
                f"Available: {', '.join(f'`{h}`' for h in fleet_state.hosts())}."
            ),
        )
        return

    if not args:
        await _post_ephemeral(
            client.chat_postEphemeral,
            channel=channel_id,
            user=user_id,
            text=(
                "ccslack: usage `/ccslack new <directory> [provider] "
                "[--worktree [branch]] [--yolo]`"
            ),
        )
        return

    raw_dir = args[0]
    provider = (args[1] if len(args) > 1 else config.provider_name).lower()
    if provider not in _SUPPORTED_PROVIDERS:
        await _post_ephemeral(
            client.chat_postEphemeral,
            channel=channel_id,
            user=user_id,
            text=(
                f"ccslack: unknown provider `{provider}`. "
                f"Pick one of: {', '.join(_SUPPORTED_PROVIDERS)}."
            ),
        )
        return

    # YOLO (permissive launch) is only meaningful for agents that expose a
    # skip-approvals flag. Requesting it for shell/pi is a no-op — warn and
    # fall back to a normal launch rather than silently dropping intent.
    if want_yolo and not has_yolo_mode(provider):
        want_yolo = False
        await _post_ephemeral(
            client.chat_postEphemeral,
            channel=channel_id,
            user=user_id,
            text=(
                f"ccslack: `--yolo` ignored — `{provider}` has no permissive "
                "launch mode (supported: claude, codex, gemini)."
            ),
        )

    work_dir = Path(raw_dir).expanduser()
    try:
        work_dir = work_dir.resolve()
    except OSError as exc:
        await _post_ephemeral(
            client.chat_postEphemeral,
            channel=channel_id,
            user=user_id,
            text=f"ccslack: bad path `{raw_dir}`: {exc}",
        )
        return
    if not work_dir.is_dir():
        await _post_ephemeral(
            client.chat_postEphemeral,
            channel=channel_id,
            user=user_id,
            text=f"ccslack: not a directory: `{work_dir}`",
        )
        return

    # Worktree flow: when --worktree is set and the repo is eligible, create
    # a fresh worktree and use its path as the session cwd. Falls back to the
    # original directory with an ephemeral warning when ineligible.
    spawn_dir = work_dir
    created_worktree_path: Path | None = None
    created_worktree_branch: str | None = None
    if want_worktree:
        # Lazy: worktree helper pulls subprocess at module top; defer cost.
        from .worktree import (
            WorktreeError,
            check_worktree_eligibility,
            create_worktree,
            slug_for_path,
            suggest_branch_name,
            validate_branch_name,
            worktree_path_for,
        )

        eligibility = check_worktree_eligibility(work_dir)
        if not eligibility.eligible:
            await _post_ephemeral(
                client.chat_postEphemeral,
                channel=channel_id,
                user=user_id,
                text=(
                    f"ccslack: `--worktree` ignored — `{work_dir}` isn't eligible "
                    f"({eligibility.reason})."
                ),
            )
        else:
            branch = worktree_branch or suggest_branch_name(None, work_dir)
            if not validate_branch_name(branch):
                await _post_ephemeral(
                    client.chat_postEphemeral,
                    channel=channel_id,
                    user=user_id,
                    text=f"ccslack: invalid branch name `{branch}`.",
                )
                return
            wt_path = worktree_path_for(work_dir, slug_for_path(branch))
            try:
                create_worktree(work_dir, branch, wt_path)
            except WorktreeError as exc:
                await _post_ephemeral(
                    client.chat_postEphemeral,
                    channel=channel_id,
                    user=user_id,
                    text=f"ccslack: worktree creation failed — {exc}",
                )
                return
            spawn_dir = wt_path
            created_worktree_path = wt_path
            created_worktree_branch = branch

    # Spawn tmux window first — fail fast if tmux isn't reachable.
    approval_mode = "yolo" if want_yolo else "normal"
    launch_command = (
        None
        if provider == "shell"
        else resolve_launch_command(provider, approval_mode=approval_mode)
    )
    success, message, window_name, window_id = await tmux_manager.create_window(
        work_dir=str(spawn_dir),
        window_name=_sanitize_channel_name(spawn_dir.name),
        start_agent=launch_command is not None,
        launch_command=launch_command,
    )
    if not success or not window_id:
        await _post_ephemeral(
            client.chat_postEphemeral,
            channel=channel_id,
            user=user_id,
            text=f"ccslack: tmux create_window failed — {message}",
        )
        return

    # Create the private channel, walking past name_taken collisions (a second
    # session on the same cwd, or prior archived channels, reserve the name).
    channel_slug = _channel_name_for(work_dir)
    bolt_client = BoltSlackClient(client)
    new_channel, create_error = await _create_unique_channel(
        bolt_client, channel_slug, window_id
    )
    if new_channel is None:
        await tmux_manager.kill_window(window_id)
        if create_error == "name_taken":
            hint = (
                " (all candidate names are taken — archive an old "
                f"`{channel_slug}*` channel or set a different "
                "`CCSLACK_CHANNEL_PREFIX`)"
            )
        elif create_error in _CHANNEL_DENIED_ERRORS:
            # The bot lacks channel-create rights here — hand off to the manual
            # bring-your-own-channel path instead of just erroring.
            kind = "public" if config.public_channels else "private"
            hint = (
                f". I'm not allowed to create the channel. Create a {kind} "
                f"channel yourself, add me to it, then run "
                f"`{config.slash_command} here {raw_dir} {provider}` there."
            )
        else:
            hint = ""
        await _post_ephemeral(
            client.chat_postEphemeral,
            channel=channel_id,
            user=user_id,
            text=f"ccslack: couldn't create channel: {create_error}{hint}",
        )
        return

    # Invite the user (best-effort).
    try:
        await bolt_client.conversations_invite(channel=new_channel, users=user_id)
    except SlackApiError as exc:
        logger.warning(
            "conversations_invite failed for %s: %s",
            user_id,
            exc.response.get("error") if exc.response else exc,
        )

    # Set channel topic + purpose so the cwd is visible at a glance.
    try:
        await bolt_client.conversations_setTopic(
            channel=new_channel, topic=f"{provider} · {work_dir}"
        )
        await bolt_client.conversations_setPurpose(
            channel=new_channel,
            purpose=(
                f"ccslack session bound to tmux window `{window_id}` "
                f"({window_name}). Type to send keys to the agent."
            ),
        )
    except SlackApiError:
        logger.debug("setTopic / setPurpose best-effort failed")

    # Bind channel→window.
    thread_router.bind_channel(new_channel, window_id, window_name=window_name)
    session_manager.set_window_provider(window_id, provider, cwd=str(spawn_dir))
    session_manager.set_window_origin(window_id, "ccslack_created")

    # Inject the ``⌘N⌘`` prompt marker for shell sessions so the passive
    # shell-output monitor can detect command boundaries and exit codes.
    # Lazy: pulls subprocess + libtmux helpers only when we actually have
    # a shell session to set up.
    if provider == "shell":
        from ..providers.shell_infra import setup_shell_prompt

        with contextlib.suppress(OSError, RuntimeError):
            await setup_shell_prompt(window_id, clear=True)
    if created_worktree_path is not None and created_worktree_branch is not None:
        session_manager.set_window_worktree(
            window_id, str(created_worktree_path), created_worktree_branch
        )

    # Post + pin the status message before the welcome — keeps Slack's pin
    # ordering newest-first.
    await ensure_status_message(
        bolt_client, new_channel, window_id, initial_state="idle"
    )

    # Announce in both channels.
    yolo_suffix = "  :warning: *YOLO*" if want_yolo else ""
    await _post_ephemeral(
        client.chat_postEphemeral,
        channel=channel_id,
        user=user_id,
        text=(
            f"ccslack: started <#{new_channel}> · `{provider}` · "
            f"tmux `{window_id}` ({work_dir}){yolo_suffix}"
        ),
    )
    try:
        yolo_line = (
            "\n:warning: *YOLO mode* — the agent runs with approvals/sandbox "
            "skipped. It can edit files and run commands without asking."
            if want_yolo
            else ""
        )
        await bolt_client.chat_postMessage(
            channel=new_channel,
            text=(
                f":sparkles: Session ready — `{provider}` in `{work_dir}`.\n"
                f"tmux window `{window_id}` ({window_name}). "
                "Type a message to send it to the agent."
                f"{yolo_line}"
            ),
        )
    except SlackApiError:
        logger.debug("welcome message post failed")

    # Offer the other allowed users a one-click join to the new private channel.
    await _post_join_offer(client, new_channel, user_id, provider, work_dir)


async def _post_join_offer(
    client,  # noqa: ANN001 — Bolt AsyncWebClient
    new_channel: str,
    creator_id: str,
    provider: str,
    work_dir: str,
) -> None:
    """Ask the *other* allowed users (in the meta channel) to join *new_channel*.

    The new session channel is private, so other users can't see it until
    invited. This posts a notice with a Join button in the meta channel; any
    meta-authorized clicker is invited to the new channel. No-op when there are
    no other allowed users or the feature is disabled.
    """
    if not config.join_offer:
        return
    others = sorted(config.allowed_users - {creator_id})
    if not others:
        return
    mentions = " ".join(f"<@{uid}>" for uid in others)
    try:
        await client.chat_postMessage(
            channel=config.meta_channel_id,
            text=f"New ccslack session <#{new_channel}> — join?",
            blocks=[
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": (
                            f":wave: <@{creator_id}> started a new session "
                            f"<#{new_channel}> (`{provider}` · `{work_dir}`).\n"
                            f"{mentions} — want to join?"
                        ),
                    },
                },
                {
                    "type": "actions",
                    "block_id": f"ccslack_join_actions:{new_channel}",
                    "elements": [
                        {
                            "type": "button",
                            "action_id": "ccslack_join_session",
                            "style": "primary",
                            "text": {
                                "type": "plain_text",
                                "text": ":inbox_tray: Join session",
                            },
                            "value": new_channel,
                        }
                    ],
                },
            ],
        )
    except SlackApiError:
        logger.debug("join-offer post failed")


async def _do_join(
    client,  # noqa: ANN001 — Bolt AsyncWebClient
    user_id: str,
    new_channel: str,
) -> None:
    """Invite *user_id* into *new_channel* and confirm via ephemeral (meta)."""
    try:
        await client.conversations_invite(channel=new_channel, users=user_id)
        text = f":inbox_tray: Added you to <#{new_channel}>."
    except SlackApiError as exc:
        error = (exc.response.get("error") if exc.response else "") or str(exc)
        if error in ("already_in_channel", "already_invited"):
            text = f"You're already in <#{new_channel}>."
        else:
            logger.warning("join invite failed for %s: %s", user_id, error)
            text = f"ccslack: couldn't add you to the session — `{error}`."
    with contextlib.suppress(SlackApiError):
        await client.chat_postEphemeral(
            channel=config.meta_channel_id, user=user_id, text=text
        )


def register_join_actions(app) -> None:  # noqa: ANN001
    """Wire the session join button (posted by `/ccslack new`)."""

    @app.action("ccslack_join_session")
    async def on_join(ack, body, client) -> None:  # noqa: ANN001
        await ack()
        user_id = body.get("user", {}).get("id", "")
        from .auth import is_meta_authorized

        if not is_meta_authorized(user_id):
            return
        new_channel = ""
        for action in body.get("actions", []) or []:
            if action.get("action_id") == "ccslack_join_session":
                new_channel = action.get("value", "")
                break
        if not new_channel:
            return
        await _do_join(client, user_id, new_channel)


async def _handle_list(client, channel_id: str, user_id: str) -> None:  # noqa: ANN001
    """Implements ``/ccslack list`` (local sessions + remote channels in a fleet)."""
    from .. import fleet_state

    bindings = list(thread_router.channel_bindings.items())
    remote = fleet_state.remote_channels()
    if not bindings and not remote:
        await _post_ephemeral(
            client.chat_postEphemeral,
            channel=channel_id,
            user=user_id,
            text="ccslack: no active sessions.",
        )
        return

    fleet = fleet_state.is_fleet()
    here = f" (`{config.host_name}`)" if fleet else ""
    lines = ["*Active ccslack sessions*"]
    if bindings:
        lines.append(f"_local{here}_" if fleet else "")
    for ch_id, window_id in bindings:
        view = session_manager.view_window(window_id)
        display = thread_router.get_display_name(window_id)
        provider = view.provider_name if view else "?"
        cwd = view.cwd if view else ""
        lines.append(
            f"• <#{ch_id}> · `{provider}` · `{window_id}` ({display}) — `{cwd}`"
        )
    if remote:
        # Detail (provider/cwd) lives on the owning worker; show channel + host.
        lines.append("_remote_")
        for ch_id, host in sorted(remote.items(), key=lambda kv: kv[1]):
            lines.append(f"• <#{ch_id}> · host `{host}`")
    await _post_ephemeral(
        client.chat_postEphemeral,
        channel=channel_id,
        user=user_id,
        text="\n".join(line for line in lines if line),
    )


async def _handle_rename(
    client,  # noqa: ANN001
    channel_id: str,
    user_id: str,
    args: list[str],
) -> None:
    """Implements ``/ccslack rename <new-name>`` — rename THIS session's channel.

    Only meaningful inside a bound session channel: the channel being renamed is
    the one the command was issued from. The requested name is sanitised to a
    Slack-legal slug before the ``conversations.rename`` call.
    """
    window_id = thread_router.get_window_for_channel(channel_id)
    if window_id is None:
        await _post_ephemeral(
            client.chat_postEphemeral,
            channel=channel_id,
            user=user_id,
            text=(
                "ccslack: `rename` only works inside a bound session channel "
                "(it renames the channel you run it from)."
            ),
        )
        return

    raw = " ".join(args).strip()
    if not raw:
        await _post_ephemeral(
            client.chat_postEphemeral,
            channel=channel_id,
            user=user_id,
            text=f"ccslack: usage `{config.slash_command} rename <new-name>`",
        )
        return

    slug = _sanitize_channel_name(raw)
    bolt_client = BoltSlackClient(client)
    try:
        await bolt_client.conversations_rename(channel=channel_id, name=slug)
    except SlackApiError as exc:
        error = exc.response.get("error") if exc.response else str(exc)
        if error == "name_taken":
            text = (
                f"ccslack: a channel named `{slug}` already exists — "
                "pick a different name."
            )
        else:
            text = f"ccslack: rename failed — {error}"
        await _post_ephemeral(
            client.chat_postEphemeral,
            channel=channel_id,
            user=user_id,
            text=text,
        )
        return

    await _post_ephemeral(
        client.chat_postEphemeral,
        channel=channel_id,
        user=user_id,
        text=f"ccslack: renamed this channel to `#{slug}`.",
    )


_CHANNEL_REF_RE = re.compile(r"<#([A-Z0-9]+)(?:\|[^>]*)?>")


# Friendly aliases → canonical notification mode.
_MUTE_ALIASES = {
    "all": "all",
    "on": "all",
    "errors": "errors_only",
    "errors_only": "errors_only",
    "err": "errors_only",
    "off": "muted",
    "muted": "muted",
    "mute": "muted",
}


_RESTORE_ALIASES = {
    "continue": "continue",
    "cont": "continue",
    "c": "continue",
    "resume": "resume",
    "r": "resume",
    "fresh": "fresh",
    "new": "fresh",
    "f": "fresh",
}


async def _handle_restore(
    client,  # noqa: ANN001
    channel_id: str,
    user_id: str,
    args: list[str],
) -> None:
    """``/ccslack restore [continue|resume|fresh]`` — respawn this channel's agent.

    Reuses the *current* channel (never creates a new one). Two cases:

      * **bound channel** — the binding still points at a (now-dead) window:
        respawn from its remembered provider / cwd / session id.
      * **unbound channel** — the binding was lost (reboot, state reset) but
        this is still a ccslack session channel: recover provider + cwd from
        the channel's own topic and re-adopt it.

    Modes: ``continue`` (default — latest session), ``resume`` (remembered /
    discovered session id), ``fresh`` (clean session).
    """
    mode = "continue"
    if args:
        resolved = _RESTORE_ALIASES.get(args[0].lower())
        if resolved is None:
            await _post_ephemeral(
                client.chat_postEphemeral,
                channel=channel_id,
                user=user_id,
                text=(
                    f"ccslack: unknown restore mode `{args[0]}` — "
                    "pick `continue`, `resume`, or `fresh`."
                ),
            )
            return
        mode = resolved

    # Lazy: recovery pulls provider + status helpers.
    from .recovery import (
        _latest_session_id_for,
        _same_cwd,
        recover_channel_context,
        restore_in_channel,
        restore_window,
    )

    window_id = thread_router.get_window_for_channel(channel_id)

    # --- bound channel: restore from the dead window's remembered state -----
    if window_id is not None:
        live = await tmux_manager.find_window_by_id(window_id)
        if live is not None:
            # The id is live, but tmux recycles window ids across restarts.
            # Confirm this live window is really THIS channel's session before
            # refusing: compare its cwd to the channel topic (the canonical
            # session cwd). A mismatch means the binding is stale (the id was
            # reused by an unrelated window) — unbind and re-adopt from the
            # topic instead of refusing or killing the wrong window.
            context = await recover_channel_context(client, channel_id)
            if context is None or _same_cwd(live.cwd, context[1]):
                await _post_ephemeral(
                    client.chat_postEphemeral,
                    channel=channel_id,
                    user=user_id,
                    text=(
                        f"ccslack: window `{window_id}` is still alive — restore "
                        "is for dead sessions. Use `/ccslack kill` first to start "
                        "over."
                    ),
                )
                return
            logger.warning(
                "restore: channel %s binding -> %s is stale (live cwd %r != "
                "topic %r); re-adopting from topic",
                channel_id,
                window_id,
                live.cwd,
                context[1],
            )
            thread_router.unbind_channel(channel_id)
            window_id = None
        else:
            new_window_id = await restore_window(
                client, channel_id, window_id, mode=mode, announce=True
            )
            if new_window_id is None:
                await _post_ephemeral(
                    client.chat_postEphemeral,
                    channel=channel_id,
                    user=user_id,
                    text=f"ccslack restore ({mode}): respawn failed (check logs).",
                )
            return

    # --- unbound channel: re-adopt from the channel's topic -----------------
    context = await recover_channel_context(client, channel_id)
    if context is None:
        await _post_ephemeral(
            client.chat_postEphemeral,
            channel=channel_id,
            user=user_id,
            text=(
                "ccslack: this channel has no binding and its topic doesn't look "
                "like a ccslack session (`<provider> · <cwd>`). Restore can't "
                "recover it — start a new session with `/ccslack new <dir>` in "
                "the meta channel."
            ),
        )
        return
    provider, cwd = context
    session_id = _latest_session_id_for(provider, cwd) if mode == "resume" else ""
    new_window_id = await restore_in_channel(
        client,
        channel_id,
        provider=provider,
        cwd=cwd,
        session_id=session_id,
        mode=mode,
        old_window_id=None,
        announce=True,
    )
    if new_window_id is None:
        await _post_ephemeral(
            client.chat_postEphemeral,
            channel=channel_id,
            user=user_id,
            text=f"ccslack restore ({mode}): re-adopt failed (check logs).",
        )


async def _handle_chat(
    client,  # noqa: ANN001
    channel_id: str,
    user_id: str,
    args: list[str],
) -> None:
    """``/ccslack chat [topic]`` — start a human-only thread (replies skip tmux).

    Posts a parent message and marks its thread so replies underneath are NOT
    forwarded to the agent — a side channel for the team to discuss without
    typing into the session.
    """
    if thread_router.get_window_for_channel(channel_id) is None:
        await _post_ephemeral(
            client.chat_postEphemeral,
            channel=channel_id,
            user=user_id,
            text="ccslack: `chat` only works inside a bound session channel.",
        )
        return

    topic = " ".join(args).strip()
    header = (
        ":speech_balloon: *Chat thread* — reply in this thread to talk with the "
        "team. Messages here are *not* sent to the agent."
    )
    if topic:
        header += f"\n>{topic}"

    try:
        result = await client.chat_postMessage(channel=channel_id, text=header)
    except SlackApiError as exc:
        logger.warning(
            "chat: postMessage failed: %s",
            exc.response.get("error") if exc.response else exc,
        )
        return
    ts = result.get("ts") if hasattr(result, "get") else result["ts"]
    if ts:
        thread_router.mark_chat_thread(channel_id, ts)


async def _handle_here(
    client,  # noqa: ANN001
    channel_id: str,
    user_id: str,
    args: list[str],
) -> None:
    """``/ccslack here <dir> [provider]`` — bind THIS channel to a fresh session.

    The bring-your-own-channel path: a human creates the channel (public, in
    office mode), adds the bot, and runs this to attach a tmux session — used
    when the bot isn't allowed to create channels itself.
    """
    if thread_router.get_window_for_channel(channel_id) is not None:
        await _post_ephemeral(
            client.chat_postEphemeral,
            channel=channel_id,
            user=user_id,
            text=(
                "ccslack: this channel is already a session. Use "
                f"`{config.slash_command} kill` to end it, or "
                f"`{config.slash_command} restore` if its window died."
            ),
        )
        return
    if not args:
        await _post_ephemeral(
            client.chat_postEphemeral,
            channel=channel_id,
            user=user_id,
            text=f"ccslack: usage `{config.slash_command} here <directory> [provider]`",
        )
        return

    raw_dir = args[0]
    provider = (args[1] if len(args) > 1 else config.provider_name).lower()
    if provider not in _SUPPORTED_PROVIDERS:
        await _post_ephemeral(
            client.chat_postEphemeral,
            channel=channel_id,
            user=user_id,
            text=(
                f"ccslack: unknown provider `{provider}`. "
                f"Pick one of: {', '.join(_SUPPORTED_PROVIDERS)}."
            ),
        )
        return

    work_dir = Path(raw_dir).expanduser()
    try:
        work_dir = work_dir.resolve()
    except OSError as exc:
        await _post_ephemeral(
            client.chat_postEphemeral,
            channel=channel_id,
            user=user_id,
            text=f"ccslack: bad path `{raw_dir}`: {exc}",
        )
        return
    if not work_dir.is_dir():
        await _post_ephemeral(
            client.chat_postEphemeral,
            channel=channel_id,
            user=user_id,
            text=f"ccslack: not a directory: `{work_dir}`",
        )
        return

    # Reuse the restore core: it spawns the window, binds THIS channel, sets
    # provider/origin, and posts the pinned status message.
    from .recovery import restore_in_channel

    new_window_id = await restore_in_channel(
        client,
        channel_id,
        provider=provider,
        cwd=str(work_dir),
        session_id="",
        mode="fresh",
        old_window_id=None,
        window_name=_sanitize_channel_name(work_dir.name),
        announce=False,
    )
    if new_window_id is None:
        await _post_ephemeral(
            client.chat_postEphemeral,
            channel=channel_id,
            user=user_id,
            text="ccslack: couldn't start the session (check logs).",
        )
        return

    # Best-effort topic so topic-based restore works later (no-op if denied).
    bolt_client = BoltSlackClient(client)
    with contextlib.suppress(SlackApiError):
        await bolt_client.conversations_setTopic(
            channel=channel_id, topic=f"{provider} · {work_dir}"
        )
    with contextlib.suppress(SlackApiError):
        await bolt_client.chat_postMessage(
            channel=channel_id,
            text=(
                f":sparkles: Session ready — `{provider}` in `{work_dir}` "
                f"(tmux `{new_window_id}`). Type a message to send it to the agent."
            ),
        )


async def _handle_grant(
    client,  # noqa: ANN001
    channel_id: str,
    user_id: str,
    args: list[str],
    *,
    grant: bool,
) -> None:
    """``/ccslack adduser|removeuser @user …`` — per-channel access (ALLOWED_USERS only)."""
    verb = "adduser" if grant else "removeuser"
    if thread_router.get_window_for_channel(channel_id) is None:
        await _post_ephemeral(
            client.chat_postEphemeral,
            channel=channel_id,
            user=user_id,
            text=f"ccslack: `{verb}` only works inside a bound session channel.",
        )
        return

    from .auth import is_meta_authorized

    if not is_meta_authorized(user_id):
        await _post_ephemeral(
            client.chat_postEphemeral,
            channel=channel_id,
            user=user_id,
            text="ccslack: only `ALLOWED_USERS` can change channel access.",
        )
        return

    targets = _parse_user_ids(args)
    if not targets:
        await _post_ephemeral(
            client.chat_postEphemeral,
            channel=channel_id,
            user=user_id,
            text=f"ccslack: usage `{config.slash_command} {verb} @user [@user …]`.",
        )
        return

    if grant:
        changed = [u for u in targets if thread_router.grant_user(channel_id, u)]
        unchanged = [u for u in targets if u not in changed]
        done_word, skip_word = "granted access to", "already had access:"
    else:
        changed = [u for u in targets if thread_router.revoke_user(channel_id, u)]
        unchanged = [u for u in targets if u not in changed]
        done_word, skip_word = "revoked access from", "wasn't granted:"

    parts: list[str] = []
    if changed:
        parts.append(f"{done_word} " + ", ".join(f"<@{u}>" for u in changed))
    if unchanged:
        parts.append(f"{skip_word} " + ", ".join(f"<@{u}>" for u in unchanged))
    with contextlib.suppress(SlackApiError):
        await client.chat_postMessage(
            channel=channel_id,
            text=":white_check_mark: " + "; ".join(parts) + " for this session.",
        )


async def _handle_users(client, channel_id: str, user_id: str) -> None:  # noqa: ANN001
    """``/ccslack users`` — list per-channel grants for this session."""
    if thread_router.get_window_for_channel(channel_id) is None:
        await _post_ephemeral(
            client.chat_postEphemeral,
            channel=channel_id,
            user=user_id,
            text="ccslack: `users` only works inside a bound session channel.",
        )
        return
    grants = thread_router.list_grants(channel_id)
    if grants:
        listing = ", ".join(f"<@{u}>" for u in grants)
        text = (
            f"Granted in this session: {listing}\n"
            "(plus everyone in `ALLOWED_USERS`)."
        )
    else:
        text = (
            "No per-channel grants yet. Only `ALLOWED_USERS` can drive this "
            f"session — add others with `{config.slash_command} adduser @user`."
        )
    await _post_ephemeral(
        client.chat_postEphemeral, channel=channel_id, user=user_id, text=text
    )


_DURATION_RE = re.compile(r"^(\d+(?:\.\d+)?)\s*([smhd])?$")
_UNIT_SECONDS = {"s": 1.0, "m": 60.0, "h": 3600.0, "d": 86400.0}


def _parse_duration(text: str, *, default_unit: str = "h") -> float | None:
    """Parse ``30m`` / ``1.5h`` / ``2`` (default hours) → seconds. None if bad."""
    match = _DURATION_RE.match(text.strip().lower())
    if not match:
        return None
    return float(match.group(1)) * _UNIT_SECONDS[match.group(2) or default_unit]


async def _handle_purge(
    client,  # noqa: ANN001
    channel_id: str,
    user_id: str,
    args: list[str],
) -> None:
    """``/ccslack purge [N | all | since <dur>]`` — delete ccslack's own output.

    Never touches the user's typed messages, the pinned status message, or
    ``/ccslack chat`` threads (those are never recorded for purging).
    """
    if thread_router.get_window_for_channel(channel_id) is None:
        await _post_ephemeral(
            client.chat_postEphemeral,
            channel=channel_id,
            user=user_id,
            text="ccslack: `purge` only works inside a bound session channel.",
        )
        return

    from . import purge as purge_mod

    count: int | None = None
    since_seconds: float | None = None
    if not args or args[0].lower() == "all":
        pass
    elif args[0].lower() == "since":
        since_seconds = _parse_duration(args[1]) if len(args) > 1 else None
        if since_seconds is None:
            await _post_ephemeral(
                client.chat_postEphemeral,
                channel=channel_id,
                user=user_id,
                text=f"ccslack: usage `{config.slash_command} purge since <30m|2h|1d>`.",
            )
            return
    elif args[0].isdigit():
        count = int(args[0])
    else:
        await _post_ephemeral(
            client.chat_postEphemeral,
            channel=channel_id,
            user=user_id,
            text=(
                f"ccslack: usage `{config.slash_command} purge "
                "[N | all | since <dur>]`."
            ),
        )
        return

    deleted = await purge_mod.purge(
        client, channel_id, count=count, since_seconds=since_seconds
    )
    await _post_ephemeral(
        client.chat_postEphemeral,
        channel=channel_id,
        user=user_id,
        text=f":wastebasket: Purged {deleted} message(s).",
    )


async def _handle_autopurge(
    client,  # noqa: ANN001
    channel_id: str,
    user_id: str,
    args: list[str],
) -> None:
    """``/ccslack autopurge [off | Xh]`` — auto-delete output older than X hours."""
    if thread_router.get_window_for_channel(channel_id) is None:
        await _post_ephemeral(
            client.chat_postEphemeral,
            channel=channel_id,
            user=user_id,
            text="ccslack: `autopurge` only works inside a bound session channel.",
        )
        return

    from . import purge as purge_mod

    if not args:
        hours = purge_mod.get_autopurge(channel_id)
        state = f"every {hours:g}h" if hours > 0 else "off"
        await _post_ephemeral(
            client.chat_postEphemeral,
            channel=channel_id,
            user=user_id,
            text=f"ccslack: autopurge is *{state}* for this session.",
        )
        return

    if args[0].lower() in ("off", "0", "none"):
        purge_mod.set_autopurge(channel_id, None)
        await _post_ephemeral(
            client.chat_postEphemeral,
            channel=channel_id,
            user=user_id,
            text=":recycle: autopurge *off* for this session.",
        )
        return

    seconds = _parse_duration(args[0])
    if seconds is None or seconds <= 0:
        await _post_ephemeral(
            client.chat_postEphemeral,
            channel=channel_id,
            user=user_id,
            text=f"ccslack: usage `{config.slash_command} autopurge [off | 1.5h | 30m]`.",
        )
        return
    hours = seconds / 3600.0
    purge_mod.set_autopurge(channel_id, hours)
    await _post_ephemeral(
        client.chat_postEphemeral,
        channel=channel_id,
        user=user_id,
        text=(
            f":recycle: autopurge *on* — output is deleted after {hours:g}h "
            "(the pinned status + chat threads are kept)."
        ),
    )


async def _handle_mute(
    client,  # noqa: ANN001
    channel_id: str,
    user_id: str,
    args: list[str],
) -> None:
    """``/ccslack mute [all|errors|off]`` — set or cycle notify mode for this channel."""
    window_id = thread_router.get_window_for_channel(channel_id)
    if window_id is None:
        await _post_ephemeral(
            client.chat_postEphemeral,
            channel=channel_id,
            user=user_id,
            text="ccslack: `mute` only works inside a bound session channel.",
        )
        return

    if args:
        alias = args[0].lower()
        mode = _MUTE_ALIASES.get(alias)
        if mode is None:
            await _post_ephemeral(
                client.chat_postEphemeral,
                channel=channel_id,
                user=user_id,
                text=(
                    f"ccslack: unknown mode `{alias}` — pick `all`, `errors`, or `off`."
                ),
            )
            return
        session_manager.set_notification_mode(window_id, mode)
    else:
        mode = session_manager.cycle_notification_mode(window_id)

    labels = {
        "all": ":speaker: *all* — every transcript message posts here",
        "errors_only": ":warning: *errors only* — only error-like text + tool flows",
        "muted": ":mute: *muted* — text suppressed; tool flows still post",
    }
    await _post_ephemeral(
        client.chat_postEphemeral,
        channel=channel_id,
        user=user_id,
        text=f"ccslack: notify mode → {labels.get(mode, mode)}",
    )


def _resolve_kill_target(raw: str, *, from_channel: str) -> tuple[str, str] | None:
    """Resolve a kill-target arg into ``(channel_id, window_id)`` or ``None``.

    Accepted forms:
      * Empty → use ``from_channel`` (caller invoked ``/ccslack kill`` inside
        the session channel).
      * ``<#C0123|name>`` — Slack channel mention.
      * Bare ``C0123…`` — channel ID.
      * ``@12`` — tmux window ID; reverse-resolved to channel.
    """
    if not raw:
        window_id = thread_router.get_window_for_channel(from_channel)
        if window_id is None:
            return None
        return from_channel, window_id

    match = _CHANNEL_REF_RE.fullmatch(raw)
    if match:
        channel_id = match.group(1)
        window_id = thread_router.get_window_for_channel(channel_id)
        return (channel_id, window_id) if window_id else None

    if raw.startswith("C") and raw[1:].isalnum():
        window_id = thread_router.get_window_for_channel(raw)
        return (raw, window_id) if window_id else None

    if is_window_id(raw):
        channel_id = thread_router.get_channel_for_window(raw)
        return (channel_id, raw) if channel_id else None

    return None


async def _kill_one(client, channel_id: str, window_id: str) -> str:  # noqa: ANN001
    """Tear down one session. Returns a human-readable line for reporting."""
    bolt_client = BoltSlackClient(client)
    display = thread_router.get_display_name(window_id)

    # Clear status message first so it doesn't linger in the channel history.
    await clear_status_message(bolt_client, channel_id, window_id)

    try:
        await tmux_manager.kill_window(window_id)
    except OSError, RuntimeError:
        logger.exception("kill_window failed for %s", window_id)

    thread_router.unbind_channel(channel_id)
    window_store.remove_window(window_id)

    # Lazy: polling cleanup helper.
    try:
        from .polling.coordinator import forget_window

        forget_window(window_id)
    except ImportError:
        pass

    # Drop any open tool-call thread state for the channel.
    from .messaging_pipeline.turn_threads import clear_channel

    clear_channel(channel_id)
    thread_router.clear_chat_threads(channel_id)
    thread_router.clear_channel_grants(channel_id)
    from .purge import forget_channel as _purge_forget
    _purge_forget(channel_id)

    try:
        await bolt_client.conversations_archive(channel=channel_id)
    except SlackApiError as exc:
        error = exc.response.get("error") if exc.response else str(exc)
        return f":warning: <#{channel_id}> ({display}) — archive failed: `{error}`"

    return f":wastebasket: killed <#{channel_id}> ({display}, `{window_id}`)"


async def _handle_kill(
    client,  # noqa: ANN001
    channel_id: str,
    user_id: str,
    args: list[str],
) -> None:
    """Implements ``/ccslack kill [target | --all --confirm]``."""
    slash = config.slash_command

    if args and args[0] == "--all":
        if channel_id != config.meta_channel_id:
            await _post_ephemeral(
                client.chat_postEphemeral,
                channel=channel_id,
                user=user_id,
                text=(
                    f"ccslack: `{slash} kill --all` only works in the meta "
                    f"channel (<#{config.meta_channel_id}>)."
                ),
            )
            return
        if "--confirm" not in args[1:]:
            count = len(thread_router.channel_bindings)
            await _post_ephemeral(
                client.chat_postEphemeral,
                channel=channel_id,
                user=user_id,
                text=(
                    f"ccslack: would kill *{count}* session(s). "
                    f"Re-run as `{slash} kill --all --confirm` to proceed."
                ),
            )
            return
        targets = list(thread_router.channel_bindings.items())
        if not targets:
            await _post_ephemeral(
                client.chat_postEphemeral,
                channel=channel_id,
                user=user_id,
                text="ccslack: no active sessions.",
            )
            return
        results = [await _kill_one(client, ch, wid) for ch, wid in targets]
        await _post_ephemeral(
            client.chat_postEphemeral,
            channel=channel_id,
            user=user_id,
            text="\n".join(results),
        )
        return

    target_arg = args[0] if args else ""
    if not target_arg and channel_id == config.meta_channel_id:
        await _post_ephemeral(
            client.chat_postEphemeral,
            channel=channel_id,
            user=user_id,
            text=(
                f"ccslack: from the meta channel, specify a target — "
                f"`{slash} kill <#channel>` or `{slash} kill @window`."
            ),
        )
        return

    resolved = _resolve_kill_target(target_arg, from_channel=channel_id)
    if resolved is None:
        await _post_ephemeral(
            client.chat_postEphemeral,
            channel=channel_id,
            user=user_id,
            text=(
                f"ccslack: couldn't resolve `{target_arg or 'this channel'}` to "
                "an active session."
            ),
        )
        return

    target_channel, target_window = resolved
    result = await _kill_one(client, target_channel, target_window)

    # Report from the meta channel (since the target channel is now archived).
    report_channel = (
        config.meta_channel_id if target_channel == channel_id else channel_id
    )
    await _post_ephemeral(
        client.chat_postEphemeral,
        channel=report_channel,
        user=user_id,
        text=result,
    )


# Aliases the user can pass to ``/ccslack toolcalls``.
_TOOLCALLS_ALIASES = {
    "shown": "shown",
    "show": "shown",
    "on": "shown",
    "hidden": "hidden",
    "hide": "hidden",
    "off": "hidden",
    "default": "default",
    "auto": "default",
}


async def _handle_toolcalls(
    client,  # noqa: ANN001
    channel_id: str,
    user_id: str,
    args: list[str],
) -> None:
    """``/ccslack toolcalls [shown|hidden|default]`` — cycle or set tool-call visibility."""
    window_id = thread_router.get_window_for_channel(channel_id)
    if window_id is None:
        await _post_ephemeral(
            client.chat_postEphemeral,
            channel=channel_id,
            user=user_id,
            text="ccslack: `toolcalls` only works inside a bound session channel.",
        )
        return

    if args:
        alias = args[0].lower()
        mode = _TOOLCALLS_ALIASES.get(alias)
        if mode is None:
            await _post_ephemeral(
                client.chat_postEphemeral,
                channel=channel_id,
                user=user_id,
                text=(
                    f"ccslack: unknown mode `{alias}` — pick "
                    "`shown`, `hidden`, or `default`."
                ),
            )
            return
        session_manager.set_tool_call_visibility(window_id, mode)
    else:
        mode = session_manager.cycle_tool_call_visibility(window_id)

    labels = {
        "shown": ":wrench: *shown* — every tool_use + tool_result posts",
        "hidden": ":no_entry_sign: *hidden* — tool_use + tool_result suppressed",
        "default": (
            ":gear: *default* — follows global "
            f"`CCSLACK_HIDE_TOOL_CALLS` (currently `{config.hide_tool_calls}`)"
        ),
    }
    await _post_ephemeral(
        client.chat_postEphemeral,
        channel=channel_id,
        user=user_id,
        text=f"ccslack: tool-call visibility → {labels.get(mode, mode)}",
    )


_THREAD_ALIASES = {
    "on": "on",
    "yes": "on",
    "off": "off",
    "no": "off",
    "flat": "off",
    "default": "default",
    "auto": "default",
}


async def _handle_thread(
    client,  # noqa: ANN001
    channel_id: str,
    user_id: str,
    args: list[str],
) -> None:
    """``/ccslack thread [on|off|default]`` — group tool chains into a thread."""
    window_id = thread_router.get_window_for_channel(channel_id)
    if window_id is None:
        await _post_ephemeral(
            client.chat_postEphemeral,
            channel=channel_id,
            user=user_id,
            text="ccslack: `thread` only works inside a bound session channel.",
        )
        return

    if args:
        alias = args[0].lower()
        mode = _THREAD_ALIASES.get(alias)
        if mode is None:
            await _post_ephemeral(
                client.chat_postEphemeral,
                channel=channel_id,
                user=user_id,
                text=(
                    f"ccslack: unknown mode `{alias}` — pick `on`, `off`, or `default`."
                ),
            )
            return
        session_manager.set_thread_tool_calls(window_id, mode)
    else:
        mode = session_manager.cycle_thread_tool_calls(window_id)

    labels = {
        "on": ":thread: *on* — tool chains grouped under a thread parent",
        "off": ":heavy_minus_sign: *off* — tool calls post flat in the channel",
        "default": (
            ":gear: *default* — follows global "
            f"`CCSLACK_THREAD_TOOL_CALLS` (currently `{config.thread_tool_calls}`)"
        ),
    }
    await _post_ephemeral(
        client.chat_postEphemeral,
        channel=channel_id,
        user=user_id,
        text=f"ccslack: tool-call threading → {labels.get(mode, mode)}",
    )


_STATUS_EMOJI = {
    "active": ":large_green_circle:",
    "idle": ":large_yellow_circle:",
    "done": ":white_check_mark:",
    "dead": ":x:",
}


async def _handle_sessions(client, channel_id: str, user_id: str) -> None:  # noqa: ANN001
    """``/ccslack sessions`` — Block Kit dashboard with per-row Kill button."""
    bindings = list(thread_router.channel_bindings.items())
    if not bindings:
        await _post_ephemeral(
            client.chat_postEphemeral,
            channel=channel_id,
            user=user_id,
            text="ccslack: no active sessions.",
        )
        return

    blocks: list[dict[str, Any]] = [
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f"*Active ccslack sessions* ({len(bindings)})",
            },
        },
        {"type": "divider"},
    ]
    for ch_id, window_id in bindings:
        view = session_manager.view_window(window_id)
        ws = window_store.window_states.get(window_id)
        provider = (view.provider_name if view else "") or "?"
        cwd = (view.cwd if view else "") or "?"
        state = (ws.status_state if ws else "idle") or "idle"
        emoji = _STATUS_EMOJI.get(state, ":grey_question:")
        display = thread_router.get_display_name(window_id)
        blocks.append(
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": (
                        f"{emoji} <#{ch_id}> · `{provider}` · `{window_id}` "
                        f"({display})\n`{cwd}`"
                    ),
                },
                "accessory": {
                    "type": "button",
                    "action_id": "ccslack_dashboard_kill",
                    "style": "danger",
                    "text": {"type": "plain_text", "text": ":wastebasket: Kill"},
                    "value": f"{ch_id}|{window_id}",
                    "confirm": {
                        "title": {"type": "plain_text", "text": "Kill session?"},
                        "text": {
                            "type": "mrkdwn",
                            "text": f"Kills tmux `{window_id}` and archives <#{ch_id}>.",
                        },
                        "confirm": {"type": "plain_text", "text": "Kill"},
                        "deny": {"type": "plain_text", "text": "Cancel"},
                    },
                },
            }
        )

    await _post_ephemeral(
        client.chat_postEphemeral,
        channel=channel_id,
        user=user_id,
        text=f"Active ccslack sessions: {len(bindings)}",
        blocks=blocks,
    )


# `/ccslack yolo [on|off]` argument aliases. Default (no arg) is "on".
_YOLO_ON_ALIASES = frozenset({"", "on", "yolo", "skip"})
_YOLO_OFF_ALIASES = frozenset({"off", "normal", "safe", "no"})


def _yolo_launch_cmd(provider: str, target_mode: str, session_id: str) -> str:
    """Build the relaunch command for switching *provider* to *target_mode*.

    ``target_mode`` is ``"yolo"`` (skip-approvals flags) or ``"normal"``. The
    provider's ``--continue`` args are appended so the restart resumes the same
    conversation.
    """
    from .recovery import _build_launch_args_for

    launch_cmd = resolve_launch_command(provider, approval_mode=target_mode)
    continue_args = _build_launch_args_for(provider, session_id, "continue")
    return f"{launch_cmd} {continue_args}".strip() if continue_args else launch_cmd


async def _handle_yolo(
    client,  # noqa: ANN001
    channel_id: str,
    user_id: str,
    args: list[str],
) -> None:
    """``/ccslack yolo [on|off]`` — restart the agent in YOLO or normal mode.

    No arg / ``on`` switches to YOLO (skip-approvals); ``off`` / ``normal``
    switches back to approvals-required. Sends a confirm message; the actual
    Ctrl-C + relaunch happens on the ``ccslack_yolo_confirm`` button click.
    """
    arg = args[0].lower() if args else ""
    if arg in _YOLO_OFF_ALIASES:
        target_mode = "normal"
    elif arg in _YOLO_ON_ALIASES:
        target_mode = "yolo"
    else:
        await _post_ephemeral(
            client.chat_postEphemeral,
            channel=channel_id,
            user=user_id,
            text=f"ccslack: usage `{config.slash_command} yolo [on|off]`.",
        )
        return

    window_id = thread_router.get_window_for_channel(channel_id)
    if window_id is None:
        await _post_ephemeral(
            client.chat_postEphemeral,
            channel=channel_id,
            user=user_id,
            text="ccslack: `yolo` only works inside a bound session channel.",
        )
        return

    live = await tmux_manager.find_window_by_id(window_id)
    if live is None:
        await _post_ephemeral(
            client.chat_postEphemeral,
            channel=channel_id,
            user=user_id,
            text="ccslack: the session window is dead — use `/ccslack restore` first.",
        )
        return

    view = session_manager.view_window(window_id)
    provider = (view.provider_name if view else "") or config.provider_name

    if target_mode == "yolo" and not has_yolo_mode(provider):
        await _post_ephemeral(
            client.chat_postEphemeral,
            channel=channel_id,
            user=user_id,
            text=f"ccslack: `{provider}` doesn't have a YOLO/skip-approvals mode.",
        )
        return

    # Intentionally NOT gated on view.approval_mode: that flag is ccslack's
    # persisted belief and drifts from reality whenever the agent is restarted
    # outside this flow, so it can't be trusted. The action is explicit and
    # confirmed below, and re-running it is an idempotent restart.

    # Build the command preview so the user can see exactly what will run.
    full_cmd = _yolo_launch_cmd(
        provider, target_mode, (view.session_id or "") if view else ""
    )

    if target_mode == "yolo":
        header = (
            f":warning: *Switch to YOLO mode?*\n"
            f"This will `Ctrl-C` the running `{provider}` process and restart it "
            f"as:\n```{full_cmd}```\n"
            "The agent will run with approvals/sandbox skipped."
        )
        fallback = f":warning: Switch `{provider}` to YOLO mode?"
        confirm_text = ":zap: Confirm YOLO"
        confirm_style = "danger"
    else:
        header = (
            f":lock: *Switch back to normal mode?*\n"
            f"This will `Ctrl-C` the running `{provider}` process and restart it "
            f"as:\n```{full_cmd}```\n"
            "The agent will require approvals again."
        )
        fallback = f":lock: Switch `{provider}` to normal mode?"
        confirm_text = ":lock: Confirm normal"
        confirm_style = "primary"

    await client.chat_postMessage(
        channel=channel_id,
        text=fallback,
        blocks=[
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": header},
            },
            {
                "type": "actions",
                "block_id": f"ccslack_yolo_actions:{window_id}",
                "elements": [
                    {
                        "type": "button",
                        "action_id": "ccslack_yolo_confirm",
                        "style": confirm_style,
                        "text": {"type": "plain_text", "text": confirm_text},
                        # Value encodes the target mode so the confirm handler
                        # knows which way to switch.
                        "value": f"{window_id}|{target_mode}",
                    },
                    {
                        "type": "button",
                        "action_id": "ccslack_yolo_cancel",
                        "text": {"type": "plain_text", "text": "Cancel"},
                        "value": window_id,
                    },
                ],
            },
        ],
    )


def register_yolo_actions(app) -> None:  # noqa: ANN001
    """Wire the YOLO confirm / cancel button actions."""

    @app.action("ccslack_yolo_confirm")
    async def on_yolo_confirm(ack, body, client) -> None:  # noqa: ANN001
        await ack()
        user_id = body.get("user", {}).get("id", "")
        channel_id = body.get("channel", {}).get("id", "")
        message_ts = (body.get("message") or {}).get("ts", "")

        from .auth import is_authorized

        if not is_authorized(user_id, channel_id):
            return

        raw_value = ""
        for action in body.get("actions", []) or []:
            if action.get("action_id") == "ccslack_yolo_confirm":
                raw_value = action.get("value", "")
                break
        # Value is "<window_id>|<target_mode>"; older messages carry just the id.
        window_id, _, mode_part = raw_value.partition("|")
        target_mode = "normal" if mode_part == "normal" else "yolo"
        if not window_id:
            return

        live = await tmux_manager.find_window_by_id(window_id)
        if live is None:
            if message_ts and channel_id:
                await client.chat_update(
                    channel=channel_id,
                    ts=message_ts,
                    text="ccslack: window died before the mode switch.",
                    blocks=[],
                )
            return

        view = session_manager.view_window(window_id)
        provider = (view.provider_name if view else "") or config.provider_name
        mode_label = "YOLO" if target_mode == "yolo" else "normal"

        full_cmd = _yolo_launch_cmd(
            provider, target_mode, (view.session_id or "") if view else ""
        )

        # Exit the current agent process. A single Ctrl-C only interrupts the
        # running task — Claude/Codex keep their REPL up and need another
        # Ctrl-C to quit — so press until the pane is actually back at a shell.
        # Otherwise the launch command below would be typed into the agent.
        exited = await tmux_manager.interrupt_agent_to_shell(window_id)
        if not exited:
            if message_ts and channel_id:
                await client.chat_update(
                    channel=channel_id,
                    ts=message_ts,
                    text=(
                        f":warning: Couldn't exit the running `{provider}` "
                        f"session (it ignored repeated Ctrl-C). {mode_label} "
                        "switch aborted — try `/ccslack kill` then "
                        "`/ccslack restore`."
                    ),
                    blocks=[],
                )
            return
        # Relaunch with the target mode + continue flags in the same tmux window.
        await tmux_manager.send_keys(window_id, full_cmd, literal=False, enter=True)

        session_manager.set_window_approval_mode(window_id, target_mode)

        if message_ts and channel_id:
            badge = ":zap:" if target_mode == "yolo" else ":lock:"
            verb = "activated" if target_mode == "yolo" else "restored"
            await client.chat_update(
                channel=channel_id,
                ts=message_ts,
                text=(
                    f"{badge} *{mode_label} mode {verb}* — restarted "
                    f"`{provider}` as `{full_cmd}`."
                ),
                blocks=[],
            )

        # Refresh the pinned status message to reflect the new mode.
        # Lazy: status helpers pull session_manager.
        from .status import update_status

        bolt_client = BoltSlackClient(client)
        await update_status(bolt_client, channel_id, window_id, "idle")

    @app.action("ccslack_yolo_cancel")
    async def on_yolo_cancel(ack, body, client) -> None:  # noqa: ANN001
        await ack()
        channel_id = body.get("channel", {}).get("id", "")
        message_ts = (body.get("message") or {}).get("ts", "")
        if message_ts and channel_id:
            await client.chat_update(
                channel=channel_id,
                ts=message_ts,
                text=":x: Mode switch cancelled.",
                blocks=[],
            )


def register_dashboard_actions(app) -> None:  # noqa: ANN001
    """Wire the dashboard Kill button (called by handlers.registry)."""

    @app.action("ccslack_dashboard_kill")
    async def on_dashboard_kill(ack, body, client) -> None:  # noqa: ANN001
        await ack()
        user_id = body.get("user", {}).get("id", "")
        # Dashboard is posted in the meta channel; killing other people's
        # sessions is a meta-level action and stays restricted to the
        # global allow-list.
        from .auth import is_meta_authorized

        if not is_meta_authorized(user_id):
            return
        target = ""
        for action in body.get("actions", []) or []:
            if action.get("action_id") == "ccslack_dashboard_kill":
                target = action.get("value", "")
                break
        if "|" not in target:
            return
        target_channel, target_window = target.split("|", 1)
        result = await _kill_one(client, target_channel, target_window)
        # Reply ephemerally in the channel where the dashboard was posted.
        dash_channel = body.get("channel", {}).get("id", "")
        if dash_channel:
            await _post_ephemeral(
                client.chat_postEphemeral,
                channel=dash_channel,
                user=user_id,
                text=result,
            )


async def create_session(
    *,
    client,  # noqa: ANN001
    meta_channel_id: str,
    user_id: str,
    raw_dir: str,
    provider: str,
    want_worktree: bool,
    worktree_branch: str | None,
    want_yolo: bool = False,
) -> None:
    """Public entry point shared by ``_handle_new`` (slash) and the modal.

    Builds the CLI-style args list and dispatches to ``_handle_new`` so all
    validation, channel creation, worktree, and binding logic stays in one place.
    """
    args = [raw_dir, provider]
    if want_worktree:
        args.append("--worktree")
        if worktree_branch:
            args.append(worktree_branch)
    if want_yolo:
        args.append("--yolo")
    await _handle_new(client, meta_channel_id, user_id, args)


__all__ = [
    "create_session",
    "register",
    "register_dashboard_actions",
    "register_yolo_actions",
]
