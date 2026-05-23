# Configuration

Everything ccslack reads at startup, plus per-channel settings persisted to
state.

---

## Environment variables

`.env` is loaded from two locations (first match wins): the current working
directory first, then `$CCSLACK_DIR` (default `~/.ccslack`). CLI flags
override env vars where applicable.

### Required

| Variable | Description |
|---|---|
| `SLACK_BOT_TOKEN` | Bot User OAuth Token (`xoxb-…`). From Slack app → Install App. |
| `SLACK_APP_TOKEN` | App-Level Token (`xapp-…`) with `connections:write` scope. From Slack app → Basic Information. Required because ccslack uses Socket Mode. |
| `SLACK_META_CHANNEL_ID` | Channel ID of the meta channel (`C…`). The bot must be a member. |
| `ALLOWED_USERS` | Comma-separated Slack user IDs (`U…`). These users can spawn sessions and manage from the meta channel. |

### Slash command

| Variable | Default | Description |
|---|---|---|
| `CCSLACK_SLASH_COMMAND` | `/ccslack` | The slash command name ccslack listens for. Set this if `/ccslack` collides with another app in your workspace. Validation: leading `/`, ≤32 chars, `[a-z0-9_-]`. |

### Storage + tmux

| Variable | Default | Description |
|---|---|---|
| `CCSLACK_DIR` | `~/.ccslack` | Config + state directory. |
| `CCSLACK_TMUX_SESSION` | `ccslack` | Tmux session ccslack manages. If you run ccslack from inside an existing tmux session, that session is used instead (auto-detected). |
| `TMUX_SESSION_NAME` | — | Legacy alias for `CCSLACK_TMUX_SESSION`. |
| `TMUX_EXTERNAL_PATTERNS` | empty | Comma-separated glob patterns for external tmux session discovery. |

### Provider defaults

| Variable | Default | Description |
|---|---|---|
| `CCSLACK_PROVIDER` | `claude` | Default provider for `/ccslack new` when no provider is specified. One of `claude` `codex` `gemini` `pi` `shell`. |
| `CCSLACK_CLAUDE_COMMAND` | `claude` | Override the launch command. Useful for wrappers like `ce`, `cc-mirror`, `zai`. |
| `CCSLACK_CODEX_COMMAND` | `codex` | … |
| `CCSLACK_GEMINI_COMMAND` | `gemini` | … |
| `CLAUDE_CONFIG_DIR` | `~/.claude` | Used when you wrap Claude with a different config directory. Affects hook install, command discovery, session monitoring. |

### Polling + monitor cadence

| Variable | Default | Description |
|---|---|---|
| `MONITOR_POLL_INTERVAL` | `1.0` | SessionMonitor poll interval (seconds). Min 0.5. |
| `CCSLACK_STATUS_POLL_INTERVAL` | `1.0` | Status polling tick (seconds). |
| `CCSLACK_LIVE_VIEW_INTERVAL` | `5` | (Reserved — live-view feature not yet ported.) |
| `CCSLACK_LIVE_VIEW_TIMEOUT` | `300` | (Reserved.) |
| `CCSLACK_SCREENSHOT_HISTORY` | `500` | Scrollback lines retained for shell prompt-marker context. Note: the `/screenshot` button itself captures only the visible viewport (matches ccgram); this value affects only shell capture context. |

### Status + UI

| Variable | Default | Description |
|---|---|---|
| `CCSLACK_STATUS_MODE` | `system` | Status emoji colour scheme. `system` = 🟢 working / 🟡 idle. `user` = 🟢 ready / 🟡 working. Invalid values fall back to `system`. |
| `CCSLACK_HIDE_TOOL_CALLS` | `false` | Global default for tool-use / tool-result visibility. Per-channel `/ccslack toolcalls` overrides this. Default `false` (show) to match ccgram. |

### Shell sessions

| Variable | Default | Description |
|---|---|---|
| `CCSLACK_PROMPT_MODE` | `wrap` | Shell prompt-marker mode. (Reserved — proper shell pipeline not yet ported.) |
| `CCSLACK_PROMPT_MARKER` | `ccslack` | Shell prompt-marker text. |

### LLM (optional)

LLM provider used for completion summaries + shell NL→command translation
(once the proper shell pipeline lands). Empty = disabled.

| Variable | Default | Description |
|---|---|---|
| `CCSLACK_LLM_PROVIDER` | empty | One of `openai` `xai` `deepseek` `anthropic` `groq` `ollama`. |
| `CCSLACK_LLM_API_KEY` | empty | API key. Falls back to provider env vars (`XAI_API_KEY`, `OPENAI_API_KEY`, etc.). |
| `CCSLACK_LLM_BASE_URL` | empty | Override for self-hosted or proxy endpoints. |
| `CCSLACK_LLM_MODEL` | provider-default | Model ID. |
| `CCSLACK_LLM_TEMPERATURE` | `0.1` | Sampling temperature for command generation / summarisation. |

### Voice (optional)

(Reserved — voice flows not yet wired into Slack handlers, but the
transcription module is ported.)

| Variable | Default | Description |
|---|---|---|
| `CCSLACK_WHISPER_PROVIDER` | empty | `openai` or `groq`. |
| `CCSLACK_WHISPER_API_KEY` | empty | Falls back to `OPENAI_API_KEY` / `GROQ_API_KEY`. |
| `CCSLACK_WHISPER_BASE_URL` | empty | Override endpoint. |
| `CCSLACK_WHISPER_MODEL` | provider-default | Whisper model. |
| `CCSLACK_WHISPER_LANGUAGE` | empty | Force language; omit for auto-detect. |
| `CCSLACK_TTS_PROVIDER` | empty | `edge` (free) or `openai`. Requires `pip install ccslack[tts]` for `edge`. |
| `CCSLACK_TTS_VOICE` | `en-US-EmmaMultilingualNeural` | Voice ID. |
| `CCSLACK_TTS_MODEL` | `gpt-4o-mini-tts` | OpenAI TTS model. |
| `CCSLACK_TTS_API_KEY` | empty | API key, falls back to `OPENAI_API_KEY`. |

### Lifecycle / autoclose

| Variable | Default | Description |
|---|---|---|
| `AUTOCLOSE_DONE_MINUTES` | `30` | (Reserved — autoclose for done sessions not yet ported.) |
| `AUTOCLOSE_DEAD_MINUTES` | `10` | (Reserved.) |
| `CCSLACK_PANE_LIFECYCLE_NOTIFY` | `false` | Default for per-window pane created/closed notifications. |
| `CCSLACK_SHOW_HIDDEN_DIRS` | `false` | Show hidden (dot) dirs in the directory browser (reserved for future modal expansion). |

### Send command

| Variable | Default | Description |
|---|---|---|
| `CCSLACK_SEND_SEARCH_DEPTH` | `5` | Max directory depth for future `/ccslack send` glob search (current implementation accepts exact paths only). |
| `CCSLACK_SEND_MAX_RESULTS` | `50` | Max results returned by `/ccslack send` search. |

### Inter-agent messaging (reserved)

| Variable | Default | Description |
|---|---|---|
| `CCSLACK_MSG_AUTO_SPAWN` | `false` | Allow agents to spawn each other without human approval. |
| `CCSLACK_MSG_MAX_WINDOWS` | `10` | Hard cap on swarm size. |
| `CCSLACK_MSG_WAIT_TIMEOUT` | `60` | Inter-agent reply wait timeout (seconds). |
| `CCSLACK_MSG_SPAWN_TIMEOUT` | `300` | Spawn approval timeout (seconds). |
| `CCSLACK_MSG_SPAWN_RATE` | `3` | Per-window spawns/hour. |
| `CCSLACK_MSG_RATE_LIMIT` | `10` | Per-window messages/5 minutes. |

---

## State files

All state lives under `$CCSLACK_DIR` (default `~/.ccslack`):

| File | Written by | Used for |
|---|---|---|
| `state.json` | Bot (debounced atomic write) | `channel_bindings`, `window_states`, `window_display_names`, per-user preferences, per-channel status_message_ts |
| `session_map.json` | `ccslack hook` subprocess (after every SessionStart) | `tmux_session:window_id` → session info (`session_id`, `cwd`, `window_name`, `transcript_path`, `provider_name`) |
| `events.jsonl` | `ccslack hook` subprocess | Append-only event log of every hook event. Bot reads incrementally via byte-offset. |
| `monitor_state.json` | Bot | Per-session byte offset into the agent's JSONL transcript (so the bot tails new lines after restart). |
| `mailbox/` | (reserved) | Inter-agent messaging inboxes (not yet wired). |
| `.env` | You | Configuration overrides. Loaded second after CWD `.env`. |

### `state.json` shape

```json
{
  "channel_bindings": {
    "C0BCDEFG": "@7",
    "C0HIJKLM": "@12"
  },
  "window_display_names": {
    "@7": "my-project",
    "@12": "another-repo"
  },
  "window_states": {
    "@7": {
      "session_id": "uuid-…",
      "cwd": "/home/me/code/my-project",
      "window_name": "my-project",
      "provider_name": "claude",
      "notification_mode": "all",
      "tool_call_visibility": "default",
      "status_message_ts": "1700000000.000100",
      "status_state": "idle"
    }
  },
  "user_window_offsets": { … },
  "user_dir_favorites": { … }
}
```

Persistence is debounced (~0.5 s) and atomic (temp file + rename). The
bot calls `session_manager.flush_state()` on shutdown to force a final
write.

### `session_map.json` shape

```json
{
  "ccslack:@7": {
    "session_id": "01234567-8901-2345-6789-0123456789ab",
    "cwd": "/home/me/code/my-project",
    "window_name": "my-project",
    "transcript_path": "/home/me/.claude/projects/foo.jsonl",
    "provider_name": "claude"
  }
}
```

Each entry corresponds to one bound tmux window. The session_id changes
after `/clear`; the bot detects this on the next poll cycle and cleans
up the old session's state.

---

## Per-channel settings

Each session channel has settings persisted on its `WindowState`:

| Field | Values | Set by |
|---|---|---|
| `notification_mode` | `all` / `errors_only` / `muted` | `/ccslack mute …` |
| `tool_call_visibility` | `default` / `shown` / `hidden` | `/ccslack toolcalls …` |
| `status_state` | `active` / `idle` / `done` / `dead` | Polling loop + hook events |
| `status_message_ts` | Slack `ts` of the pinned status message | Bot on first status post |
| `worktree_path`, `worktree_branch` | Absolute path + branch | `/ccslack new --worktree` |

These survive bot restarts.

---

## CLI flags

The CLI defers to env vars for most things; the few flags ccslack accepts:

```
ccslack --config-dir <path>     # equivalent to CCSLACK_DIR
ccslack --version, -v
ccslack --help
ccslack hook --install / --uninstall / --status [--provider X]
```

There's no `--token` flag — Slack tokens are env-only so they don't show
up in `ps` output.
