"""Discover Claude Code commands and expose builtin/CC command lists.

Scans three sources to build the command list:
  1. Built-in CC commands (always present)
  2. User-invocable skills from ~/.claude/skills/
  3. Custom commands from ~/.claude/commands/

In ccslack the actual chat-level slash commands are declared in the Slack app
manifest, not registered at runtime — so this module only provides discovery
(used by the forward layer and the /commands handler), not a `register` call.

Core components:
  - ``CC_BUILTINS`` — provider-consumed dict of builtin Claude Code commands.
  - ``CCCommand`` dataclass: name, safe_name, description, source.
  - ``discover_cc_commands()`` — filesystem scanner.
  - ``get_cc_name()`` — reverse lookup from sanitized safe-name to CC name.
"""

import structlog
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Literal, cast

from ccslack.command_catalog import (
    command_catalog,
    discover_user_defined_commands,
    parse_frontmatter as _parse_frontmatter,
)
from ccslack.providers.base import AgentProvider

logger = structlog.get_logger()

# Built-in Claude Code commands. Refreshed from code.claude.com/docs/en/commands.
# /new is bot-native (excluded); /resume collides with bot-native /resume (excluded).
CC_BUILTINS: dict[str, str] = {
    "agents": "↗ Manage subagent configurations",
    "background": "↗ Detach session as background agent",
    "branch": "↗ Fork conversation at current point",
    "clear": "↗ Clear conversation context",
    "compact": "↗ Summarize history to free context",
    "context": "↗ Show context window usage",
    "copy": "↗ Copy last response to clipboard",
    "cost": "↗ Show session cost (alias /usage)",
    "diff": "↗ Open interactive diff viewer",
    "doctor": "↗ Diagnose Claude Code installation",
    "effort": "↗ Adjust thinking effort level",
    "export": "↗ Export conversation as text",
    "feedback": "↗ Submit feedback or bug report",
    "help": "↗ Show Claude Code help",
    "init": "↗ Initialize CLAUDE.md in project",
    "loop": "↗ Run prompt repeatedly on a schedule",
    "mcp": "↗ Manage MCP server connections",
    "memory": "↗ Edit CLAUDE.md memory files",
    "model": "↗ Select model and effort",
    "permissions": "↗ Manage tool permissions",
    "plan": "↗ Enter plan mode",
    "rc": "↗ Start remote control (alias)",
    "recap": "↗ Summarize current session",
    "release-notes": "↗ View changelog",
    "remote-control": "↗ Start remote control session",
    "review": "↗ Review pull request locally",
    "rewind": "↗ Rewind conversation or code",
    "security-review": "↗ Analyze pending changes for security",
    "settings": "↗ Open Settings interface",
    "skills": "↗ List available skills",
    "statusline": "↗ Configure status line",
    "status": "↗ Show session status",
    "tasks": "↗ Manage background tasks",
    "theme": "↗ Change color theme (picker)",
    "tui": "↗ Switch terminal UI renderer",
    "usage": "↗ Show usage and cost stats",
    "verify": "↗ Build and run app to verify change",
}

# Bot-native commands. Surfaced by /commands in Slack; Slack itself uses the
# top-level `/ccslack <sub>` slash command for these (see app manifest).
_BOT_COMMANDS: list[tuple[str, str]] = [
    ("new", "Create new Claude session"),
    ("commands", "List commands for this channel's provider"),
    ("history", "Message history for this channel"),
    ("sessions", "Sessions dashboard"),
    ("resume", "Browse and resume past sessions"),
    ("screenshot", "Capture terminal screenshot"),
    ("live", "Open auto-refreshing terminal view"),
    ("panes", "List panes in this window"),
    ("restore", "Recover a dead channel"),
    ("sync", "Audit and fix state"),
    ("unbind", "Unbind this channel"),
    ("recall", "Recall recent commands"),
    ("toolbar", "Show action toolbar"),
    ("verbose", "Toggle tool call batching"),
    ("upgrade", "Upgrade ccslack and restart"),
]

# Max length we'll surface in a Slack menu description.
_MAX_DESCRIPTION_LEN = 256


@dataclass(frozen=True, slots=True)
class CCCommand:
    """A discovered Claude Code command."""

    name: str  # Original CC name (e.g. "spec:work", "committing-code")
    safe_name: str  # Sanitized for chat-platform slash menus (e.g. "spec_work")
    description: str
    source: Literal["builtin", "skill", "command"]


def _sanitize_slash_name(name: str) -> str:
    """Sanitize a CC command name for chat-platform slash menus.

    Lowercase, [a-z0-9_], max 32 chars. Empty string for unrepresentable names.
    """
    sanitized = name.lower().replace("-", "_").replace(":", "_")
    sanitized = "".join(c for c in sanitized if c.isalnum() or c == "_")
    return sanitized[:32]


def _cc_desc(desc: str) -> str:
    """Ensure description has ↗ prefix for CC-forwarded commands."""
    return desc if desc.startswith("↗") else f"↗ {desc}"


def parse_frontmatter(path: Path) -> dict[str, str]:
    """Compatibility wrapper around provider-agnostic frontmatter parser."""
    return _parse_frontmatter(path)


def discover_cc_commands(claude_dir: Path | None = None) -> list[CCCommand]:
    """Scan filesystem for CC commands.

    Sources (in order):
      1. Built-in commands (CC_BUILTINS)
      2. Skills: {claude_dir}/skills/*/SKILL.md (user-invocable only)
      3. Custom commands: {claude_dir}/commands/{group}/*.md

    Commands with empty sanitized names are skipped.
    """
    if claude_dir is None:
        # Lazy: config singleton is wired late at startup; importing at top
        # would freeze test overrides that monkeypatch config attrs.
        from ccslack.config import config

        claude_dir = config.claude_config_dir

    commands: list[CCCommand] = []

    for name, desc in CC_BUILTINS.items():
        commands.append(
            CCCommand(
                name=name,
                safe_name=_sanitize_slash_name(name),
                description=desc,
                source="builtin",
            )
        )

    for cmd in discover_user_defined_commands(claude_dir):
        safe = _sanitize_slash_name(cmd.name)
        if not safe:
            continue
        commands.append(
            CCCommand(
                name=cmd.name,
                safe_name=safe,
                description=_cc_desc(cmd.description),
                source=cast(Literal["builtin", "skill", "command"], cmd.source),
            )
        )

    return commands


# Module-level cache (safe_name → cc_name, first-wins).
_name_map: dict[str, str] = {}


def _provider_base_dir(claude_dir: Path | None = None) -> str:
    """Resolve base dir for provider command discovery."""
    # Lazy: config singleton accessed only when this helper actually runs
    from ccslack.config import config as _cfg

    return str(claude_dir) if claude_dir else str(_cfg.claude_config_dir)


def discover_provider_commands(
    provider: AgentProvider,
    claude_dir: Path | None = None,
) -> list[CCCommand]:
    """Discover commands for one provider as CCCommand entries."""
    base_dir = _provider_base_dir(claude_dir)
    valid_sources = {"builtin", "skill", "command"}
    discovered = command_catalog.get_provider_commands(provider, base_dir)
    commands: list[CCCommand] = []
    for cmd in discovered:
        if not cmd.name:
            continue
        commands.append(
            CCCommand(
                name=cmd.name,
                safe_name=_sanitize_slash_name(cmd.name),
                description=_cc_desc(cmd.description),
                source=cast(
                    Literal["builtin", "skill", "command"],
                    cmd.source if cmd.source in valid_sources else "command",
                ),
            )
        )
    return commands


def get_provider_command_map(
    provider: AgentProvider,
    claude_dir: Path | None = None,
) -> dict[str, str]:
    """Build safe_name -> original command mapping for a provider."""
    mapping: dict[str, str] = {}
    for cmd in discover_provider_commands(provider, claude_dir):
        if cmd.safe_name and cmd.safe_name not in mapping:
            mapping[cmd.safe_name] = cmd.name
    return mapping


def get_provider_supported_commands(
    provider: AgentProvider,
    claude_dir: Path | None = None,
) -> set[str]:
    """Return normalized slash commands supported by a provider."""
    supported: set[str] = set()
    for name in get_provider_command_map(provider, claude_dir).values():
        token = name if name.startswith("/") else f"/{name}"
        supported.add(token.lower())
    for name in provider.capabilities.builtin_commands:
        if not name:
            continue
        token = name if name.startswith("/") else f"/{name}"
        supported.add(token.lower())
    return supported


def _refresh_cache(
    claude_dir: Path | None = None,
    provider: AgentProvider | None = None,
    providers: Iterable[AgentProvider] | None = None,
) -> list[CCCommand]:
    """Re-discover commands and update the cache."""
    global _name_map

    if providers is not None:
        commands = []
        for discovered_provider in providers:
            commands.extend(discover_provider_commands(discovered_provider, claude_dir))
    elif provider is not None:
        commands = discover_provider_commands(provider, claude_dir)
    else:
        commands = discover_cc_commands(claude_dir)
    new_map: dict[str, str] = {}
    for cmd in commands:
        if cmd.safe_name not in new_map:
            new_map[cmd.safe_name] = cmd.name
    _name_map = new_map
    return commands


def get_cc_name(safe_name: str) -> str | None:
    """Look up the original CC command name from a sanitized safe name."""
    return _name_map.get(safe_name)


__all__ = [
    "CC_BUILTINS",
    "CCCommand",
    "discover_cc_commands",
    "discover_provider_commands",
    "get_cc_name",
    "get_provider_command_map",
    "get_provider_supported_commands",
    "parse_frontmatter",
]
