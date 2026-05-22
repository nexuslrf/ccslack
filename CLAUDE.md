# CLAUDE.md

ccslack manages AI coding agents from Slack via tmux. Each Slack channel binds to one tmux window running one agent CLI (Claude Code, Codex, Gemini, Pi, or shell).

Transplanted from [ccgram](../ccgram). The two codebases share architecture and design decisions; ccslack only swaps the chat transport (Slack Bolt Socket Mode in place of python-telegram-bot polling).

Tech stack: Python 3.14, slack-bolt (async), slack-sdk, libtmux, pyte, uv.

## Commands

```bash
make check                    # fmt + lint + typecheck + tests
make fmt                      # format
make lint                     # MUST pass before commit
make typecheck                # MUST be 0 errors before commit
make test                     # unit
make test-integration         # real tmux, fs
ccslack                       # run bot
ccslack hook --install        # install Claude Code hooks
ccslack status                # local state
ccslack doctor [--fix]        # validate / auto-fix setup
```

## Core Constraints (mirror ccgram)

- **1 Channel = 1 Window = 1 Session.** Routing keyed by Slack channel ID (e.g. `C0123ABC`) → tmux window ID (`@0`, `@12`). Window names are display labels. Same directory may have multiple windows.
- **Channel-per-session, private only.** All session channels are created as private channels (invite-only). One designated **meta channel** holds the new-session slash command and the session index.
- **Socket Mode** transport. No HTTP webhook server. The bot connects out over websocket to Slack; no inbound port needed.
- **Live view: text only.** ccgram replaces image attachments with `editMessageMedia`; Slack can't replace files on a message, so ccslack uses text in a code block, updated via `chat.update`. On-demand screenshots use `files.upload v2`.
- **Pinned status message, not channel rename.** Slack rate-limits channel renames; ccslack pins one status message per session channel and edits it on state change.
- **Block Kit, not MessageEntity.** Rich formatting goes through Slack's `rich_text` blocks. Drop the markdown→entities pipeline.
- **Hook-based session tracking.** Same Claude Code hooks as ccgram, writing `session_map.json` and `events.jsonl`. The hook entry binary `ccslack hook` is a drop-in for `ccgram hook`.

## Code Conventions

- Every `.py` starts with a module-level docstring (one-sentence summary first line, then responsibilities; clear within 10 lines).
- Slack: Block Kit blocks > attachments. Use `chat_update` for in-place edits. `action_id` and `value` budget is 255 chars (vs Telegram's 64) — use it, don't cram.
- Full variable names: `window_id` not `wid`, `channel_id` not `cid`, `session_id` not `sid`.
- Catch specific exceptions (`OSError`, `ValueError`, `slack_sdk.errors.SlackApiError`); never bare `except Exception`.
- Tests: `tests/ccslack/` (unit), `tests/integration/`, `tests/e2e/`. `asyncio_mode = "auto"` — no `@pytest.mark.asyncio`. No comments or docstrings in test files.

## Configuration

Precedence: CLI flag > env var > `.env` (local > config dir) > default.

- Config dir: `~/.ccslack/` or `--config-dir` / `CCSLACK_DIR`.
- `SLACK_BOT_TOKEN`, `SLACK_APP_TOKEN`: env-only.
- `SLACK_META_CHANNEL_ID`: required; the channel where session management lives.
- `ALLOWED_USERS`: comma-separated Slack user IDs (`U...`).

State files (in config dir): `state.json` (channel bindings, window states, display names), `session_map.json` (hook-generated), `events.jsonl` (hook events), `monitor_state.json` (byte offsets), `mailbox/` (inter-agent inboxes).

## Architecture

Shared with ccgram:

- **Providers** (`providers/`) — Claude, Codex, Gemini, Pi, Shell. Untouched.
- **Tmux** (`tmux_manager.py`) — untouched.
- **SessionMonitor** (`session_monitor.py`) — polls JSONL transcripts + reads `events.jsonl`. Untouched.
- **Hook** (`hook.py`) — Claude Code hook entry that writes `session_map.json` + `events.jsonl`. Path swap only.
- **LLM / Whisper** — untouched.

Swapped:

- `slack_client.py` (was `telegram_client.py`) — `SlackClient` Protocol + `BoltSlackClient` adapter + `FakeSlackClient` recorder.
- `slack_formatting.py` (was `entity_formatting.py`) — markdown → Block Kit blocks.
- `slack_sender.py` (was `telegram_sender.py`) — `safe_post`, `safe_update`, `split_blocks`.
- `handlers/` — Slack-native handlers: meta-channel commands, text, status, screenshot, recovery.
- `thread_router.py` — channel-keyed routing instead of (user_id, thread_id).
- `bot.py` — `AsyncApp` + `AsyncSocketModeHandler` instead of PTB `Application`.

## Slack-specific design notes

- **No emoji status in channel name.** Channel renames hit rate limits fast. Instead, each session channel has one pinned status message; status changes are `chat.update`s on that message.
- **Meta channel as control plane.** All session creation flows through the meta channel via `/ccslack new` (or the bot's app shortcut). Listing active sessions = bookmarks/list in the meta channel.
- **Recovery / picker UX = Block Kit modals.** Dead-window recovery posts a banner with action buttons; resume picker opens a modal listing past sessions.
- **Inline keyboards = `actions` blocks.** Up to 25 elements per actions block. `action_id` must be unique per message.

## Migrating from ccgram

If you've used ccgram: the agent / tmux / session layers are conceptually identical. What's different is the chat surface. Read `docs/diff-from-ccgram.md` (TODO) for the per-feature mapping.
