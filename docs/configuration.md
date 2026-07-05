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
| `CCSLACK_META_SURFACE` | `channel` | Where session management lives. `channel` = the meta channel only (unchanged on upgrade). `hybrid` = the meta channel **and** the bot's DM both accept `/ccslack` commands, dashboard buttons, and the `new` modal. `dm` = the bot's DM only. Proactive notifications (join offers, SSH 2FA prompts, startup ping) still post to the meta channel for now — only commands/buttons honour this mode. For `hybrid`/`dm`, enable the bot's **Messages tab** in the manifest (`features.app_home.messages_tab_enabled: true`) so `/ccslack` is usable in the DM; commands-only DM management needs no extra `im:*` scopes (`chat:write` covers the replies). |
| `CCSLACK_PROXY` | empty | HTTP(S) proxy for **all** Slack traffic — the Socket Mode WebSocket *and* the Web API (posts/uploads), on the router and every worker. Must be an `http(s)://` URL: the slack-sdk (aiohttp) stack **doesn't support SOCKS** and **doesn't read `*_PROXY` env vars**, so this is the supported knob. For a SOCKS upstream, front it with a SOCKS→HTTP bridge, or run the process under `proxychains`. (Agent CLIs in tmux inherit your shell env separately.) |
| `CCSLACK_CHANNEL_PREFIX` | `ccslack` | Prefix for auto-created channel names: `<prefix>-<cwd-slug>`. Sanitized to Slack-legal characters. Set to empty to use just the cwd slug (e.g. `vrender` instead of `ccslack-vrender`). |
| `CCSLACK_TABLE_RENDER` | `true` | When an agent answer contains a markdown table, post a button offering to render it as an image (Slack renders markdown tables poorly). The raw text is always posted; set `false` to suppress the button. |
| `CCSLACK_JOIN_OFFER` | `true` | After `/ccslack new` creates a session, post a notice in the meta channel with a **Join** button so the other `ALLOWED_USERS` can opt into the new private channel. Set `false` to skip it. |
| `CCSLACK_PUBLIC_CHANNELS` | `false` | **Office mode.** Create **public** session channels and stop trusting channel membership for auth — instead require `ALLOWED_USERS` + per-channel `/ccslack adduser` grants. For workspaces that forbid private channels (`groups:write`). Needs `channels:manage`/`channels:history`/`channels:read` + the `message.channels` event. ⚠️ Agent output, `/send` files, and screenshots become visible to the whole workspace. See [commands](commands.md#public-office-mode). |

### Multi-host (router + workers)

Opt-in. With `CCSLACK_WORKERS` empty, `ccslack router` == standalone. Full
guide: [multi-host.md](multi-host.md).

| Variable | Default | Role | Description |
|---|---|---|---|
| `CCSLACK_HOST` | hostname | all | This machine's name in the fleet (shown in `/ccslack list`, targeted by `new --host`). |
| `CCSLACK_LINK_PORT` | `8765` | worker | Localhost port the worker's link server listens on (the router reaches it over an SSH tunnel — never exposed on the network). |
| `CCSLACK_WORKERS` | empty | router | Comma-separated `host=ssh_target` entries, e.g. `gpu1=user@gpu1,gpu2=gpu2-alias`. Empty = single-host router (standalone behaviour). |
| `CCSLACK_FLEET_NOTIFY` | `false` | router | Post host connect/disconnect lines to the meta channel. Off by default — use `/ccslack fleet` to check status on demand. |
| `CCSLACK_SSH_INTERACTIVE` | `false` | router | Run SSH tunnels under a PTY and bridge interactive auth prompts (e.g. Duo 2FA) to the meta channel for a Slack-side response (option buttons + a passcode modal), instead of the console. |
| `CCSLACK_SSH_PROMPT_RE` | *(Duo/password/2FA)* | router | Regex (searched against the prompt tail) marking "ssh is waiting for input". Tune to your server's prompt wording. |

`SLACK_APP_TOKEN` is **router-only** in a fleet — a worker receives forwarded
events instead of opening Socket Mode, so it runs without it.

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
| `CCSLACK_PROVIDER` | `claude` | Default provider for `/ccslack new` when no provider is specified. One of `claude` `codex` `gemini` `pi` `shell` `cursor`. |
| `CCSLACK_RESTORE_ON_START` | `banner` | What to do at startup for bound channels whose tmux window died (reboot / tmux restart). `banner` posts the manual Fresh/Continue/Resume/Archive recovery banner; `continue` auto-respawns and continues the latest session; `resume` auto-respawns with the remembered session id (falls back to continue); `off` does nothing (polling still posts the banner later). |
| `CCSLACK_CLAUDE_COMMAND` | `claude` | Override the launch command. Useful for wrappers like `ce`, `cc-mirror`, `zai`. |
| `CCSLACK_CODEX_COMMAND` | `codex` | … |
| `CCSLACK_GEMINI_COMMAND` | `gemini` | … |
| `CCSLACK_CURSOR_COMMAND` | `cursor-agent` | Override the [Cursor Agent CLI](https://cursor.com/docs/cli/overview) launch command. |
| `CLAUDE_CONFIG_DIR` | `~/.claude` | Used when you wrap Claude with a different config directory. Affects hook install, command discovery, session monitoring. |

> **Cursor provider notes.** `cursor-agent` must be logged in
> (`cursor-agent login`, subscription) — the interactive session ccslack drives
> uses that login, no `CURSOR_API_KEY` needed. Cursor has **no hooks**: ccslack
> discovers a session by scanning its SQLite chat store
> (`~/.cursor/chats/<md5(cwd)>/<agentId>/store.db`) and tails it for replies, so
> there is **no `ccslack hook --install` step** for Cursor. Cursor's in-TUI
> permission prompts aren't parsed yet, so for unattended use launch with YOLO
> (`--force`, via the `new` modal's YOLO checkbox or `/ccslack yolo`).

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
| `CCSLACK_THREAD_TOOL_CALLS` | `true` | Global default for grouping a turn's `tool_use` / `tool_result` / `thinking` under one threaded parent in the main channel (keeps long tool chains tidy). Plain answers + interactive prompts stay in the main channel. Per-channel `/ccslack thread [on\|off\|default]` overrides this. |

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
| `CCSLACK_SEND_SEARCH_DEPTH` | `5` | Max directory depth walked by `/ccslack send` glob / substring search. |
| `CCSLACK_SEND_MAX_RESULTS` | `50` | Max files returned by `/ccslack send` glob / substring search. |

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
| `purge.json` | Bot | Ledger of ccslack-posted output `ts` per channel + per-channel `autopurge` window, for `/ccslack purge` / `autopurge`. |
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
| `thread_tool_calls` | `default` / `on` / `off` | `/ccslack thread …` |
| `status_state` | `active` / `idle` / `done` / `dead` | Polling loop + hook events |
| `status_message_ts` | Slack `ts` of the pinned status message | Bot on first status post |
| `worktree_path`, `worktree_branch` | Absolute path + branch | `/ccslack new --worktree` |

These survive bot restarts.

---

## CLI flags

The CLI defers to env vars for most things; the few flags ccslack accepts:

```
ccslack                         # standalone bot (default)
ccslack router [--host <name>]  # multi-host router (see multi-host.md)
ccslack worker [--port N --host <name>]   # multi-host worker
ccslack --config-dir <path>     # equivalent to CCSLACK_DIR
ccslack --version, -v
ccslack --help
ccslack hook --install / --uninstall / --status [--provider X]
```

There's no `--token` flag — Slack tokens are env-only so they don't show
up in `ps` output.
