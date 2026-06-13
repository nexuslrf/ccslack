"""Application configuration — reads env vars and exposes a singleton.

Loads SLACK_BOT_TOKEN, SLACK_APP_TOKEN, SLACK_META_CHANNEL_ID, ALLOWED_USERS,
plus tmux/Claude paths and monitoring intervals from environment variables
(with .env support). .env loading priority: local .env (cwd) > $CCSLACK_DIR/.env
(default ~/.ccslack). The module-level ``config`` instance is imported by nearly
every other module.

Slack-specific keys:
  - ``SLACK_BOT_TOKEN``  (xoxb-...) — bot OAuth token for Web API calls.
  - ``SLACK_APP_TOKEN``  (xapp-...) — app-level token for Socket Mode.
  - ``SLACK_META_CHANNEL_ID`` — channel where ``/ccslack new`` lives.
  - ``ALLOWED_USERS`` — comma-separated Slack user IDs (``U...``).

Key class: Config (singleton instantiated as ``config``).
"""

import structlog
import os
from pathlib import Path

from dotenv import load_dotenv

from .utils import ccslack_dir

logger = structlog.get_logger()


def _parse_int_env(name: str, default: int) -> int:
    """Parse an integer from an env var with a clear error on bad values."""
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be a valid integer: {exc}") from exc


# Slack slash command rules — leading "/" + ≤_MAX_SLASH_BODY_CHARS chars of [a-z0-9_-].
_MAX_SLASH_BODY_CHARS = 32


def _resolve_slash_command(env_value: str) -> str:
    """Validate and normalise the slash command env input.

    Raises ``ValueError`` when Slack's command-name rules are violated.
    """
    raw = (env_value or "").strip()
    if not raw.startswith("/"):
        raw = "/" + raw
    body = raw[1:]
    if (
        not body
        or len(body) > _MAX_SLASH_BODY_CHARS
        or not all(c.isalnum() or c in "_-" for c in body.lower())
    ):
        raise ValueError(
            f"CCSLACK_SLASH_COMMAND={raw!r} is invalid; Slack requires "
            f"a leading '/', then ≤{_MAX_SLASH_BODY_CHARS} chars of [a-z0-9_-]."
        )
    return "/" + body.lower()


def _resolve_toolbar_path() -> str:
    """Resolve the toolbar TOML config path."""
    env = os.getenv("CCSLACK_TOOLBAR_CONFIG", "").strip()
    if env:
        return env
    fallback = ccslack_dir() / "toolbar.toml"
    return str(fallback) if fallback.exists() else ""


class Config:
    """Application configuration loaded from environment variables."""

    def __init__(self) -> None:
        self.config_dir = ccslack_dir()
        self.config_dir.mkdir(parents=True, exist_ok=True)

        # Load .env: local (cwd) takes priority over config_dir.
        # load_dotenv default override=False means first-loaded wins.
        for env_path in (Path(".env"), self.config_dir / ".env"):
            if env_path.is_file():
                load_dotenv(env_path)
                logger.debug("Loaded env from %s", env_path.resolve())

        self.slack_bot_token: str = os.getenv("SLACK_BOT_TOKEN") or ""
        if not self.slack_bot_token:
            raise ValueError("SLACK_BOT_TOKEN environment variable is required")

        self.slack_app_token: str = os.getenv("SLACK_APP_TOKEN") or ""
        if not self.slack_app_token:
            raise ValueError(
                "SLACK_APP_TOKEN environment variable is required (Socket Mode)"
            )

        self.meta_channel_id: str = os.getenv("SLACK_META_CHANNEL_ID") or ""
        if not self.meta_channel_id:
            raise ValueError("SLACK_META_CHANNEL_ID environment variable is required")

        allowed_users_str = os.getenv("ALLOWED_USERS", "")
        if not allowed_users_str:
            raise ValueError("ALLOWED_USERS environment variable is required")
        self.allowed_users: set[str] = {
            uid.strip() for uid in allowed_users_str.split(",") if uid.strip()
        }
        # Sanity check: Slack user IDs start with U (or W for Enterprise Grid).
        for uid in self.allowed_users:
            if not (uid.startswith(("U", "W"))):
                raise ValueError(
                    f"ALLOWED_USERS contains non-Slack user ID '{uid}'. "
                    "Expected comma-separated Slack user IDs like 'U0123ABC'."
                )

        # Slash command name (configurable to avoid collisions with other apps
        # registered in the same workspace). Must match the command registered
        # in the Slack app config exactly.
        self.slash_command: str = _resolve_slash_command(
            os.getenv("CCSLACK_SLASH_COMMAND", "/ccslack")
        )

        # Prefix for auto-created Slack channel names ("<prefix>-<cwd-slug>").
        # Set to an empty string to drop the prefix entirely. Sanitized to
        # Slack-legal characters where it is applied.
        self.channel_prefix: str = os.getenv("CCSLACK_CHANNEL_PREFIX", "ccslack")

        # Tmux session name and window naming
        self.tmux_session_name = os.getenv(
            "CCSLACK_TMUX_SESSION", os.getenv("TMUX_SESSION_NAME", "ccslack")
        )
        self.tmux_main_window_name = "__main__"
        self.own_window_id: str | None = None
        self.tmux_external_patterns: str = os.getenv("TMUX_EXTERNAL_PATTERNS", "")

        # All state files live under config_dir
        self.state_file = self.config_dir / "state.json"
        self.session_map_file = self.config_dir / "session_map.json"
        self.monitor_state_file = self.config_dir / "monitor_state.json"
        self.events_file = self.config_dir / "events.jsonl"
        self.mailbox_dir = self.config_dir / "mailbox"

        # Claude Code session monitoring configuration
        _claude_config_dir = os.getenv("CLAUDE_CONFIG_DIR")
        self.claude_config_dir: Path = (
            Path(_claude_config_dir).expanduser()
            if _claude_config_dir
            else Path.home() / ".claude"
        )
        self.claude_projects_path = self.claude_config_dir / "projects"
        self.monitor_poll_interval = max(
            0.5, float(os.getenv("MONITOR_POLL_INTERVAL", "1.0"))
        )
        self.status_poll_interval = max(
            0.5, float(os.getenv("CCSLACK_STATUS_POLL_INTERVAL", "1.0"))
        )

        # Provider selection
        self.provider_name: str = os.getenv("CCSLACK_PROVIDER", "claude")

        # Startup recovery for sessions whose tmux window vanished (reboot,
        # tmux server restart). On bootstrap, every bound channel whose window
        # is dead is handled per this mode:
        #   "banner"   (default) — post the manual Fresh/Continue/Resume/Archive
        #                          recovery banner; user picks.
        #   "continue" — auto-respawn the agent with its provider's continue
        #                flag (claude --continue, codex resume --last, …).
        #   "resume"   — auto-respawn with --resume <session_id> when a session
        #                id is known, else fall back to continue.
        #   "off"      — do nothing automatically; the polling loop still posts
        #                the banner when it later notices the dead window.
        raw_restore = os.getenv("CCSLACK_RESTORE_ON_START", "banner").strip().lower()
        self.restore_on_start: str = (
            raw_restore
            if raw_restore in ("banner", "continue", "resume", "off")
            else "banner"
        )

        # Directory browser: show hidden (dot) directories
        self.show_hidden_dirs: bool = os.getenv(
            "CCSLACK_SHOW_HIDDEN_DIRS", ""
        ).lower() in ("1", "true", "yes")

        # Whisper transcription
        self.whisper_provider: str = os.getenv("CCSLACK_WHISPER_PROVIDER", "")
        self.whisper_api_key: str = os.getenv("CCSLACK_WHISPER_API_KEY", "")
        self.whisper_base_url: str = os.getenv("CCSLACK_WHISPER_BASE_URL", "")
        self.whisper_model: str = os.getenv("CCSLACK_WHISPER_MODEL", "")
        self.whisper_language: str = os.getenv("CCSLACK_WHISPER_LANGUAGE", "")

        # Voice replies (text-to-speech)
        self.tts_provider: str = os.getenv("CCSLACK_TTS_PROVIDER", "")
        self.tts_voice: str = os.getenv(
            "CCSLACK_TTS_VOICE", "en-US-EmmaMultilingualNeural"
        )
        self.tts_model: str = os.getenv("CCSLACK_TTS_MODEL", "gpt-4o-mini-tts")
        self.tts_api_key: str = os.getenv("CCSLACK_TTS_API_KEY", "")

        self._init_shell_and_llm()
        self._init_messaging()
        self._init_live_view()
        self._init_send()
        self._init_lifecycle()
        self._init_feature_flags()

        # Status display: green=active (system POV) or green=ready (user POV).
        raw_status_mode = os.getenv("CCSLACK_STATUS_MODE", "").strip().lower()
        self.status_mode: str = (
            raw_status_mode if raw_status_mode in ("system", "user") else "system"
        )

        logger.debug(
            "Config initialized: dir=%s, token=%s..., allowed_users=%d, "
            "tmux_session=%s, meta_channel=%s",
            self.config_dir,
            self.slack_bot_token[:8],
            len(self.allowed_users),
            self.tmux_session_name,
            self.meta_channel_id,
        )

    def _init_feature_flags(self) -> None:
        # Global default for hiding tool_use/tool_result content.
        # Per-window override via WindowState.tool_call_visibility takes precedence.
        # Default is *show* (matches ccgram) so users see the agent's tool chain
        # right away; set CCSLACK_HIDE_TOOL_CALLS=true to opt back into quiet mode.
        self.hide_tool_calls: bool = os.getenv(
            "CCSLACK_HIDE_TOOL_CALLS", "false"
        ).lower() in ("1", "true", "yes")

        # When an agent answer contains a markdown table, offer a button to
        # render it as an image (Slack renders markdown tables poorly). The raw
        # text is always posted; this only controls the extra offer. Set
        # CCSLACK_TABLE_RENDER=false to suppress the button entirely.
        self.table_render_offer: bool = os.getenv(
            "CCSLACK_TABLE_RENDER", "true"
        ).lower() in ("1", "true", "yes")

        # After `/ccslack new` creates a session, post a join offer in the meta
        # channel so other allowed users can opt into the new private channel.
        # Set CCSLACK_JOIN_OFFER=false to skip it.
        self.join_offer: bool = os.getenv(
            "CCSLACK_JOIN_OFFER", "true"
        ).lower() in ("1", "true", "yes")

        # Group an agent turn's tool_use / tool_result / thinking under one
        # threaded parent message in the main channel, so long tool chains
        # don't flood the channel. Plain answers + interactive prompts stay in
        # the main channel. Per-window override via /ccslack thread.
        self.thread_tool_calls: bool = os.getenv(
            "CCSLACK_THREAD_TOOL_CALLS", "true"
        ).lower() in ("1", "true", "yes")

    def _init_messaging(self) -> None:
        self.msg_auto_spawn: bool = os.getenv("CCSLACK_MSG_AUTO_SPAWN", "").lower() in (
            "1",
            "true",
            "yes",
        )
        self.msg_max_windows: int = _parse_int_env("CCSLACK_MSG_MAX_WINDOWS", 10)
        self.msg_wait_timeout: int = _parse_int_env("CCSLACK_MSG_WAIT_TIMEOUT", 60)
        self.msg_spawn_timeout: int = _parse_int_env("CCSLACK_MSG_SPAWN_TIMEOUT", 300)
        self.msg_spawn_rate: int = _parse_int_env("CCSLACK_MSG_SPAWN_RATE", 3)
        self.msg_rate_limit: int = _parse_int_env("CCSLACK_MSG_RATE_LIMIT", 10)

    def _init_live_view(self) -> None:
        self.live_view_interval: int = max(
            1, _parse_int_env("CCSLACK_LIVE_VIEW_INTERVAL", 5)
        )
        self.live_view_timeout: int = max(
            1, _parse_int_env("CCSLACK_LIVE_VIEW_TIMEOUT", 300)
        )
        self.screenshot_history: int = max(
            50, _parse_int_env("CCSLACK_SCREENSHOT_HISTORY", 500)
        )

    def _init_shell_and_llm(self) -> None:
        self.prompt_mode = os.getenv("CCSLACK_PROMPT_MODE", "wrap")
        self.prompt_marker = os.getenv("CCSLACK_PROMPT_MARKER", "ccslack")
        self.toolbar_config_path: str = _resolve_toolbar_path()
        self.llm_provider: str = os.getenv("CCSLACK_LLM_PROVIDER", "")
        self.llm_api_key: str = os.getenv("CCSLACK_LLM_API_KEY", "")
        self.llm_base_url: str = os.getenv("CCSLACK_LLM_BASE_URL", "")
        self.llm_model: str = os.getenv("CCSLACK_LLM_MODEL", "")
        try:
            self.llm_temperature: float = float(
                os.getenv("CCSLACK_LLM_TEMPERATURE", "0.1")
            )
        except ValueError as e:
            raise ValueError(
                f"CCSLACK_LLM_TEMPERATURE must be a valid number: {e}"
            ) from e

    def _init_send(self) -> None:
        self.send_search_depth: int = _parse_int_env("CCSLACK_SEND_SEARCH_DEPTH", 5)
        self.send_max_results: int = _parse_int_env("CCSLACK_SEND_MAX_RESULTS", 50)

    def _init_lifecycle(self) -> None:
        self.autoclose_done_minutes: int = int(
            os.getenv("AUTOCLOSE_DONE_MINUTES", "30")
        )
        self.autoclose_dead_minutes: int = int(
            os.getenv("AUTOCLOSE_DEAD_MINUTES", "10")
        )
        self.pane_lifecycle_notify: bool = os.getenv(
            "CCSLACK_PANE_LIFECYCLE_NOTIFY", ""
        ).lower() in ("1", "true", "yes")

    def is_user_allowed(self, user_id: str) -> bool:
        """Check if a Slack user ID is in the allowed list."""
        return user_id in self.allowed_users


config = Config()
