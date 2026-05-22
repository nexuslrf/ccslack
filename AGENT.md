# Transplanting ccgram for Slack

## Goal: make original ccgram work with Slack

Source ccgram: `/nvmepool/ruofan/project/tools/ccgram`

## Design Choices

Since Slack's interface differs substantially from Telegram, ccslack compromises on
some features:

- **Channel-per-session** to bypass Telegram's forum-topic format. A dedicated
  **meta channel** holds session management commands and the active-session index.
  Session channels are **private** (invite-only).
- **Socket Mode** Slack bot (no inbound HTTP webhook).
- **Live view: text only.** Slack can't replace file attachments on an existing
  message — `chat.update` on a code block instead. Image screenshots are
  on-demand via `files.upload v2`.
- **Pinned status message, not channel rename.** Slack rate-limits renames; we
  pin one status message per session channel and edit it on state change.

## Walking-skeleton status: complete

All 15 walking-skeleton tasks are done. The bot can be installed in a Slack
workspace and used end-to-end to drive a Claude Code (or Codex / Gemini / Pi /
shell) session.

### What works

- `uv sync` installs deps cleanly. `make check` (fmt + lint + tests) passes.
- 13 unit tests cover formatting, sender, thread router.
- `ccslack --help / status / hook / doctor / run` all dispatch correctly.
- **Bot lifecycle** — Socket Mode connects, post-init bootstrap (resolve stale
  IDs, start session monitor, start status polling) runs in order, clean shutdown.
- **App mention health check** — `@ccslack` in the meta channel replies
  `:green_heart: ccslack online`.
- **`/ccslack help / new <dir> [provider] / list`** — slash command, ephemeral
  replies in the meta channel only.
- **`/ccslack new`** spins up a fresh tmux window with the chosen agent CLI,
  creates a private Slack channel (`#ccslack-<slug>`), invites the user, sets
  channel topic + purpose, binds channel↔window, posts a pinned status message.
- **Inbound text → tmux** — messages in bound channels go through
  `tmux_manager.send_keys`. Authorized senders get a ✓ reaction; unauthorized
  get 🚫.
- **Outbound transcript → Slack** — SessionMonitor reads JSONL transcripts,
  `message_routing.handle_new_message` posts to the bound channel with
  Block-Kit-aware `safe_post` (rich text or code block, plain-text fallback).
- **Pinned status message** with action buttons (Screenshot, Archive) —
  `chat.update`d on state transitions (`active` ⇄ `idle`, `done`, `dead`).
- **Screenshot** — `:camera:` button captures the tmux pane and uploads as PNG
  via `files_upload_v2`.
- **Archive** — `:wastebasket:` button kills tmux window, unbinds channel,
  archives Slack channel.
- **Status polling** (1 Hz) — detects dead windows, decays `active` → `idle`
  after 5 s of quiet, posts the recovery banner once on death.
- **Recovery banner** — Block Kit buttons for Fresh / Continue / Resume /
  Archive. Fresh & Continue work today; Resume uses the last known session_id
  for providers that support `--resume`.
- **Hook events** — `Stop` / `SessionEnd` / `StopFailure` flip status to
  `done` (with detail on failure).

### Module inventory (`src/ccslack/`)

80 Python files, ~20 k LOC. Layout:

- **Core** (ported from ccgram, Slack-flavored where needed): `config.py`,
  `session.py`, `session_map.py`, `session_lifecycle.py`, `session_monitor.py`,
  `session_resolver.py`, `session_query.py`, `state_persistence.py`,
  `thread_router.py`, `tmux_manager.py`, `user_preferences.py`,
  `window_state_store.py`, `window_query.py`, `window_view.py`,
  `window_resolver.py`, `claude_task_state.py`, `transcript_parser.py`,
  `transcript_reader.py`, `screen_buffer.py`, `terminal_parser.py`,
  `screenshot.py`, `expandable_quote.py`, `cc_commands.py`,
  `command_catalog.py`, `hook.py`, `mailbox.py`, `idle_tracker.py`, `utils.py`.
- **Slack transport** (new): `slack_client.py` (Protocol + Bolt adapter +
  Fake), `slack_formatting.py`, `slack_sender.py`.
- **Lifecycle** (new): `bot.py`, `bootstrap.py`, `cli.py`, `main.py`.
- **Handlers** (new):
  - `handlers/registry.py` — wiring spine.
  - `handlers/meta.py` — `/ccslack` slash command.
  - `handlers/text.py` — inbound message → tmux.
  - `handlers/status.py` — pinned status message + archive action.
  - `handlers/screenshot.py` — screenshot button + uploader.
  - `handlers/recovery.py` — dead-window banner + Fresh/Continue/Resume/Archive.
  - `handlers/hook_events.py` — Stop/SessionEnd dispatch.
  - `handlers/messaging_pipeline/message_routing.py` — outbound transcript routing.
  - `handlers/polling/coordinator.py` — status polling loop.
- **Shared, unchanged**: `providers/`, `llm/`, `whisper/`, `tts/`, `fonts/`.

## Source-of-truth mapping (Telegram → Slack)

| ccgram                          | ccslack                                          |
|---------------------------------|--------------------------------------------------|
| Forum topic                     | Private channel                                  |
| `thread_id`                     | `channel_id` (Slack `C…`)                        |
| `user_id` (int)                 | Slack user_id (`U…` str)                         |
| `MessageEntity` offsets         | Block Kit `rich_text` / `section`                |
| Inline keyboard buttons         | `actions` block with `button` elements           |
| `callback_data` (64 B)          | `action_id` + `value` (255 chars each)           |
| `answer_callback_query` (toast) | `chat_postEphemeral` / `response_url`            |
| `editMessageMedia` (live image) | text in code block via `chat_update`             |
| Topic emoji recolor             | Pinned status message edited via `chat_update`   |
| `BotCommand` menu               | Slash commands declared in app manifest          |
| Long polling                    | Socket Mode websocket                            |
| `python-telegram-bot`           | `slack-bolt[async]`                              |

## Slack app setup

OAuth bot scopes:

- `app_mentions:read`
- `chat:write`
- `commands`
- `channels:manage`, `channels:write.invites`
- `groups:write`, `groups:read`, `groups:write.invites`
- `pins:write`, `pins:read`
- `files:write`, `files:read`
- `reactions:write`

Event subscriptions: `app_mention`, `message.groups`.

Socket Mode: enable; generate an app-level token with `connections:write`.

Slash commands: register `/ccslack` (description `Manage ccslack sessions`,
usage hint `new <dir> [provider]`).

## Known follow-ups (out of skeleton scope)

- **Resume picker modal.** Currently `Resume` reuses the last known
  `session_id`; a proper picker would scan the JSONL transcripts for the
  bound `cwd` and present a `views.open` modal.
- **Tool-use / tool-result pairing.** Outbound posts are independent today;
  porting `ccgram` `tool_batch` would group tool calls with their results.
- **Per-channel message queue.** No FIFO worker yet; rate limiting is
  per-channel at the `safe_post` boundary.
- **Worktree integration.** `/ccslack new` skips the eligibility check; the
  worktree plumbing in `WindowState` is present but unused.
- **Voice messages, /history, /sessions dashboard, /toolbar.** Not ported.
- **Mini App dashboard.** Out of scope; Slack's modal + home tab cover most of
  what ccgram's Telegram Mini App provided.
- **Hook installer.** `ccslack hook --install` is currently a `sys.argv` re-dispatch
  into the original ccgram hook entry; needs its own Slack-aware install
  path that writes settings into the right Claude Code / Codex / Gemini
  config files.
- **Tests.** 13 unit tests today. Integration tests with a fake Bolt request
  pipeline (mirroring ccgram's PTB `_do_post` patch) are not yet wired.
