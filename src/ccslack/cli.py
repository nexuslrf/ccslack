"""Click CLI for ccslack — run, hook install, status, doctor."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import click
import structlog

from . import __version__

logger = structlog.get_logger()


@click.group(invoke_without_command=True)
@click.option(
    "--config-dir",
    "config_dir",
    type=click.Path(file_okay=False, dir_okay=True, path_type=Path),
    envvar="CCSLACK_DIR",
    help="Override ~/.ccslack config directory.",
)
@click.version_option(__version__, "--version", "-v")
@click.pass_context
def cli(ctx: click.Context, config_dir: Path | None) -> None:
    """Manage AI coding agents from Slack via tmux."""
    if config_dir is not None:
        import os

        os.environ["CCSLACK_DIR"] = str(config_dir)
    if ctx.invoked_subcommand is None:
        ctx.invoke(run)


@cli.command()
def run() -> None:
    """Start the Slack bot (default subcommand)."""
    # Lazy: importing bot pulls in slack-bolt at module top level, which
    # spawns the lazy connection setup. Keep it out of the CLI prologue.
    from .bot import create_app, start_socket_mode, stop_socket_mode

    async def _main() -> None:
        app = create_app()
        await start_socket_mode(app)
        try:
            # Block forever — Socket Mode runs as a background task on the loop.
            await asyncio.Event().wait()
        finally:
            await stop_socket_mode()

    try:
        asyncio.run(_main())
    except KeyboardInterrupt:
        click.echo("\nShutdown requested.")
        sys.exit(0)


@cli.command()
@click.option("--host", "host_name", default=None, help="This router host's name (default CCSLACK_HOST/hostname).")
def router(host_name: str | None) -> None:
    """Run as the multi-host router: the single Socket Mode intake + dispatcher.

    Holds the Slack connection and routes each event to the owning host's worker,
    while also running sessions locally (double role). With no workers connected
    this behaves exactly like a standalone `ccslack run`.
    """
    from . import fleet_state
    from .bot import create_app, start_event_source, stop_event_source
    from .config import config
    from .router import Router, RouterSource
    from .router_link import RouterFleet, parse_workers
    from .slack_client import BoltSlackClient
    from .slack_sender import safe_post

    async def _main() -> None:
        app = create_app()
        router_obj = Router(local_host=host_name or config.host_name)
        specs = parse_workers(config.workers_raw, config.link_port)
        fleet_state.install_router(router_obj)
        fleet_state.set_workers([(s.host, s.ssh_target) for s in specs])
        await start_event_source(app, RouterSource(app, router_obj))

        meta_client = BoltSlackClient(app.client)

        notify = None
        if config.fleet_notify:

            async def _notify(text: str) -> None:
                await safe_post(meta_client, channel=config.meta_channel_id, text=text)

            notify = _notify

        on_prompt = None
        if config.ssh_interactive:
            from .handlers.ssh_prompt import post_prompt

            async def _on_prompt(host: str, text: str, options: list) -> None:
                await post_prompt(meta_client, host, text, options)

            on_prompt = _on_prompt

        fleet = RouterFleet(router_obj, specs, notify=notify, on_prompt=on_prompt)
        fleet_state.set_session_gatherer(fleet.gather_sessions)
        await fleet.start()
        try:
            await asyncio.Event().wait()
        finally:
            await fleet.stop()
            await stop_event_source()

    try:
        asyncio.run(_main())
    except KeyboardInterrupt:
        click.echo("\nShutdown requested.")
        sys.exit(0)


@cli.command()
@click.option("--port", type=int, default=None, help="Localhost link port (default CCSLACK_LINK_PORT/8765).")
@click.option("--host", "host_name", default=None, help="Host name reported to the router (default CCSLACK_HOST/hostname).")
def worker(port: int | None, host_name: str | None) -> None:
    """Run as a multi-host worker: drive local tmux, receive events from a router.

    No Slack Socket Mode connection — the router (a separate `ccslack` on the
    app token) forwards events here over an SSH tunnel; this process posts to
    Slack directly with the bot token.
    """
    from .bot import create_app, start_event_source, stop_event_source
    from .config import config
    from .event_source import RouterLinkSource

    async def _main() -> None:
        app = create_app()
        source = RouterLinkSource(
            app,
            host=host_name or config.host_name,
            port=port if port is not None else config.link_port,
        )
        await start_event_source(app, source)
        try:
            await asyncio.Event().wait()
        finally:
            await stop_event_source()

    try:
        asyncio.run(_main())
    except KeyboardInterrupt:
        click.echo("\nShutdown requested.")
        sys.exit(0)


@cli.command()
@click.option("--install", "action", flag_value="install")
@click.option("--uninstall", "action", flag_value="uninstall")
@click.option("--status", "action", flag_value="status")
@click.option(
    "--provider",
    type=click.Choice(["claude", "codex", "gemini", "pi"]),
    default="claude",
)
def hook(action: str | None, provider: str) -> None:
    """Install / uninstall / inspect agent-CLI hooks, OR process a hook event.

    With no flag this command reads a Claude Code hook event from stdin and
    writes ``session_map.json`` / appends to ``events.jsonl``. This is the form
    invoked by the entry registered in ``~/.claude/settings.json``.

    Use ``--install`` / ``--uninstall`` / ``--status`` to manage the entries.
    """
    # Lazy: hook module pulls Claude Code config paths; only needed for this CLI.
    from .hook import hook_main

    hook_main(
        install=(action == "install"),
        uninstall=(action == "uninstall"),
        status=(action == "status"),
        provider_name=provider,
    )


@cli.command()
def status() -> None:
    """Show local ccslack state (no Slack tokens required)."""
    # Lazy: avoid hard dep on config / Slack tokens for `status`.
    import os

    os.environ.setdefault("SLACK_BOT_TOKEN", "stub")
    os.environ.setdefault("SLACK_APP_TOKEN", "stub")
    os.environ.setdefault("SLACK_META_CHANNEL_ID", "stub")
    os.environ.setdefault("ALLOWED_USERS", "U000")
    from .config import config

    click.echo(f"ccslack {__version__}")
    click.echo(f"config dir: {config.config_dir}")
    click.echo(f"state file: {config.state_file}")
    click.echo(f"session map: {config.session_map_file}")
    click.echo(f"tmux session: {config.tmux_session_name}")


@cli.command()
@click.option("--fix", is_flag=True, help="Apply fixes for detected issues.")
def doctor(fix: bool) -> None:
    """Validate ccslack setup. Walking-skeleton: stub."""
    click.echo("ccslack doctor: stub. Full implementation pending.")
    if fix:
        click.echo("(--fix has no effect in the skeleton)")


def main() -> None:
    """Entry point for the ``ccslack`` console script."""
    cli()


if __name__ == "__main__":
    main()
