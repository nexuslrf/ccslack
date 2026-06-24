# Architecture

For contributors and the curious. This doc explains *how* ccslack works:
how a Slack message becomes tmux keystrokes, how a tmux transcript becomes
a Slack message, and which design decisions made these flows tractable.

---

## High-level flow

```
┌─────────────────────┐         ┌──────────────────────────────────┐
│   Slack workspace   │ socket  │            ccslack               │
│ ┌─────────────────┐ │  mode   │ ┌──────────────────────────────┐ │
│ │ #meta channel   │◀───┐  ┌──▶│ │ Bolt AsyncApp (events, ack)  │ │
│ │ #ccslack-…      │    │  │   │ └──────────┬───────────────────┘ │
│ │ #ccslack-…      │    │  │   │            ▼                     │
│ └─────────────────┘    │  │   │ ┌──────────────────────────────┐ │
└────────────────────────┼──┼───│ │ handlers/ (Slack-side)       │ │
                         │  │   │ │  meta, text, status, toolbar │ │
                         │  │   │ │  interactive, recovery, …    │ │
                         │  │   │ └──────────┬───────────────────┘ │
                         │  │   │            ▼                     │
                         │  │   │ ┌──────────────────────────────┐ │
                         │  │   │ │ tmux_manager (libtmux + cli) │ │
                         │  │   │ └──────────┬───────────────────┘ │
                         │  │   │            ▼                     │
                         │  │   │ ┌──────────────────────────────┐ │
                         │  │   │ │ tmux session "ccslack"       │ │
                         │  │   │ │  window @7 ─ claude          │ │
                         │  │   │ │  window @12 ─ codex          │ │
                         │  │   │ │  window @14 ─ bash           │ │
                         │  │   │ └──────────┬───────────────────┘ │
                         │  │   │            ▼                     │
                         │  │   │ ┌──────────────────────────────┐ │
                         │  │   │ │ agent CLI (writes JSONL +    │ │
                         │  │   │ │ fires hook subprocess)       │ │
                         │  │   │ └──────────┬───────────────────┘ │
                         │  │   │            ▼                     │
                         │  │   │ ┌──────────────────────────────┐ │
                         │  │   │ │ ~/.ccslack/                  │ │
                         │  │   │ │  session_map.json (hook)     │ │
                         │  │   │ │  events.jsonl (hook)         │ │
                         │  │   │ │  state.json (bot)            │ │
                         │  │   │ └──────────┬───────────────────┘ │
                         │  │   │            ▼                     │
                         │  │   │ ┌──────────────────────────────┐ │
                         │  └───◀─│ SessionMonitor (tails JSONL) │ │
                         │       │ └──────────────────────────────┘ │
                         └───────│ outbound transcript routing      │
                                 └──────────────────────────────────┘
```

---

## Subsystems

The package layout in `src/ccslack/`:

```
src/ccslack/
├── config.py                  # Singleton config from env + .env
├── thread_router.py           # Channel ↔ window routing
├── session.py                 # SessionManager — wires every store + state IO
├── session_*.py, window_*.py  # Stores, resolvers, audit (read-only query layer)
├── tmux_manager.py            # libtmux wrapper (windows, panes, send-keys, capture)
├── session_monitor.py         # JSONL tail + hook events.jsonl reader
├── transcript_*.py            # JSONL parsing per provider
├── hook.py + hooks/           # ccslack hook subprocess + payload adapters
├── providers/                 # Claude / Codex / Gemini / Pi / Shell
├── llm/, whisper/, tts/       # Pluggable LLM / Whisper / TTS factories
├── screen_buffer.py           # pyte VT100 buffer
├── terminal_parser.py         # ANSI → styled segments
├── screenshot.py              # text → PNG
├── slack_client.py            # SlackClient Protocol + Bolt adapter + FakeSlackClient
├── slack_formatting.py        # mrkdwn ↔ Block Kit
├── slack_sender.py            # safe_post / safe_update / split_message
├── bot.py                     # AsyncApp factory + event-source lifecycle
├── event_source.py            # SocketModeSource / RouterLinkSource + dispatch seam
├── router.py, router_link.py, link.py, fleet_state.py   # multi-host (see multi-host.md)
├── ssh_auth.py                # SSH 2FA prompt bridge (PTY + responder registry)
├── bootstrap.py               # post-init wiring
├── cli.py, main.py            # Click CLI (run / router / worker)
├── utils.py, state_persistence.py, mailbox.py, …
└── handlers/                  # All Slack-side event handlers
    ├── auth.py
    ├── registry.py            # register_all — wiring spine
    ├── meta.py                # /ccslack dispatcher + subcommands
    ├── text.py                # Slack message → tmux send-keys
    ├── status.py              # Pinned status message + Archive
    ├── screenshot.py          # 📷 Screenshot button + uploader
    ├── toolbar.py             # 🎛️ Toolbar message + key dispatch
    ├── interactive.py         # Live picker (tool_use-driven)
    ├── new_modal.py           # Block Kit modal for /ccslack new
    ├── resume.py              # /ccslack resume picker
    ├── history.py             # /ccslack history paginator
    ├── panes.py               # /ccslack panes inspector
    ├── send.py + send_security.py  # /ccslack send + security filters
    ├── recovery.py            # Dead-window banner
    ├── ssh_prompt.py          # SSH 2FA prompt toolbar + passcode modal (router)
    ├── purge.py               # /ccslack purge + autopurge + remove buttons
    ├── table_render.py        # markdown-table → image offer
    ├── hook_events.py         # Stop / SessionEnd / StopFailure / Notification
    ├── shell_capture.py       # Pre/post pane-diff for shell sessions
    ├── worktree.py            # git worktree plumbing
    ├── polling/               # Status polling + dead-window detection
    │   ├── coordinator.py
    │   └── prompt_probe.py    # Fallback regex prompt detection
    └── messaging_pipeline/    # Outbound transcript routing
        └── message_routing.py
```

---

## Key design decisions

### 1. Channel = Window = Session

Every Slack channel binds to **exactly one** tmux window, which runs
**exactly one** agent CLI. Routing keyed by:

- `thread_router.channel_bindings: dict[channel_id, window_id]` — flat,
  workspace-unique (Slack channel IDs don't need a per-user dimension
  the way Telegram thread IDs did).
- `_window_to_channel` reverse index for O(1) inbound lookups.

The unique key is the tmux **window_id** (`@7`, `@12`), not the window
name. Names are display labels stored separately in
`window_display_names`. This lets the same directory have multiple
windows without conflict.

### 2. Socket Mode, not HTTP webhook

ccslack connects out to Slack over a websocket. No public endpoint, no
TLS, no reverse proxy. Just one process. This is the natural fit for
"a personal bot that runs alongside your agents" use case.

`bot.start_socket_mode()` opens the connection after running the
post-init bootstrap (resolve stale window IDs, start SessionMonitor,
start status polling).

### 3. SlackClient Protocol

Handlers depend on a narrow `SlackClient` Protocol (`slack_client.py`)
covering the ~25 web-API methods ccslack uses, not on the concrete
`AsyncWebClient`. `BoltSlackClient` is the production adapter;
`FakeSlackClient` is a recording fake used in unit tests. Mirrors
ccgram's `TelegramClient` / `PTBTelegramClient` / `FakeTelegramClient`
pattern.

### 4. JSONL-driven outbound, hook-driven coordination

Two parallel data streams from the agent process:

- **JSONL transcript** — every assistant message + tool call lands as
  one JSON line. `SessionMonitor` tails the file (byte-offset incremental
  reads with mtime caching). This is the primary user-visible content
  stream.
- **Hook events.jsonl** — `ccslack hook` subprocess (registered in
  `~/.claude/settings.json` etc.) appends one JSON line per hook event.
  `SessionMonitor` reads this too, for instant signals (SessionStart,
  Stop, Notification) that don't appear cleanly in the transcript.

Bot reads both, dispatches into handlers. No live polling of the agent
process itself — only the files it writes.

### 5. Tool-use ↔ tool-result pairing

`message_routing._post_or_pair`:
- On `tool_use`: post the message, remember `(channel, ts, text)` keyed
  by `tool_use_id`.
- On `tool_result` with matching `tool_use_id`: `chat.update` the
  original tool_use message to include the result inline.

Channel reads as one chunked event per tool call instead of two
disjointed messages.

### 6. Live interactive picker

The hardest UX problem in the bot, and the most ccgram-like piece.

**Detection** — three triggers:
1. **JSONL `tool_use`** of `AskUserQuestion` / `ExitPlanMode` /
   `request_user_input` — instant, exact, provider-uniform. Wired in
   `message_routing`.
2. **Claude `Notification` hook** — for permission requests that don't
   show up in JSONL until *after* the user decides. Wired in
   `hook_events`.
3. **Pane regex fallback** — `polling/prompt_probe.py` detects selector
   glyphs `❯ ▶ › →` + numbered choices, or inline `[y/N]`. Used for
   Codex's exec-approval prompt (purely TUI, no JSONL signal) and for
   shell sessions.

**Lifecycle** — `handlers/interactive.py`:
- One picker per channel (`_active: dict[channel_id, InteractiveSession]`).
- Posts a Block Kit message with arrow / control / digit / Dismiss
  buttons; key buttons reuse the toolbar's `ccslack_key:*` action
  handler.
- Background refresh task captures the pane every 0.8 s, hashes,
  `chat.update`s only when content changed.
- Closes (deletes the message — no terminal-state stub) on: matching
  tool_result, agent Stop, dead window, idle timeout, user dismiss,
  superseded by a new tool_use.

### 7. Viewport-only screenshots

`/screenshot` uses `tmux_manager.capture_pane` — the *visible viewport*
only, not the scrollback. Matches ccgram's behaviour: successive
screenshots stay the same size and focus on recent ops.

The `CCSLACK_SCREENSHOT_HISTORY` env var still exists but is used only
by shell prompt-marker context (a separate code path).

### 8. Channel-membership auth

Two-level auth via `handlers/auth.py`:
- `is_authorized(user_id, channel_id)` — trusts anyone in a *bound*
  session channel (Slack already enforces channel membership for event
  delivery in private channels, so the bot's invite *is* the auth).
  Falls back to global `ALLOWED_USERS` for unbound channels.
- `is_meta_authorized(user_id)` — always strict; used for actions that
  *create* sessions or affect every session.

### 9. Per-channel rate limiting

`slack_sender.rate_limit_send(channel_id)` enforces a 1.1 s minimum gap
between outbound messages per channel. Below Slack's tier-3 limit with
headroom.

### 10. Status state machine

`WindowState.status_state` cycles through `active → idle → done → dead`,
driven by:
- `active` — every outbound transcript message (`message_routing`)
- `idle` — `coordinator` flips active→idle after 5 s of no activity
- `done` — `hook_events._on_stop` on Stop / SessionEnd
- `dead` — `coordinator._handle_dead` when tmux window vanishes

The pinned status message (`handlers/status.py`) re-renders on each
transition via `chat.update`. Configurable colour scheme
(`CCSLACK_STATUS_MODE` = `system` | `user`).

### 11. Persistence

State writes are debounced + atomic (`state_persistence.py`): mutators
schedule a save; the save coalesces within 0.5 s and writes via
temp-file + atomic rename. Shutdown forces a final flush.

Window IDs are not stable across tmux server restarts; `session_manager.
resolve_stale_ids()` runs at startup and re-maps persisted display
names against live windows.

### 12. Pluggable event source (standalone / router / worker)

Inbound events come from a swappable `EventSource` (`event_source.py`):
`SocketModeSource` is the standalone connection; a worker uses
`RouterLinkSource` and the router `RouterSource`. The seam is
`dispatch_payload(app, payload)` — it feeds a raw Slack event into the Bolt app
exactly as the Socket Mode adapter does, so a forwarded event runs through the
identical handler stack. Because only the *inbound* socket is singular (outbound
Web API works from anywhere), a fleet needs just one intake — the **router** —
which acks Slack and routes each event to the owning host's worker over an SSH
tunnel (`router.py`, `router_link.py`, `link.py`). Standalone is unchanged.

Routing is by `channel_id` (or a `--host` directive on a slash command); the
`Router` keeps a `channel_id → host` registry fed by each worker's
`hello`/`bind`/`unbind`. The `link` protocol is mostly fire-and-forget, with one
request/reply (`sessions_req`/`sessions_rep`) so `/ccslack sessions` can merge
every host's sessions. `fleet_state` is the decoupling layer — the router
installs itself there and meta handlers (`new --host`, `list`, `fleet`,
`sessions`) consult it, degrading to "just this host" off the router. Remote
`new`/`kill` from the meta channel work by forwarding a synthetic slash command
to the owning worker. Optionally (`CCSLACK_SSH_INTERACTIVE`) a tunnel runs under
a PTY (`ssh_auth.py`) so an interactive auth prompt (Duo 2FA) is bridged to the
meta channel and answered from Slack (`handlers/ssh_prompt.py`).

Full design + setup: [multi-host.md](multi-host.md).

---

## Test strategy

- **Unit** (`tests/ccslack/`) — `FakeSlackClient` + `monkeypatch` for env
  vars. No real Slack, no real tmux. 65 tests cover formatting, sender,
  routing, picker, shell capture, prober, auth, slash-command
  validation, kill resolver, thread router.
- **Integration** (intentionally not present yet) — would need a Bolt
  request fake to replay event payloads.
- **E2E** (intentionally not present) — would spin up real tmux + real
  agent CLIs.

Run:

```bash
make test         # unit tests
make check        # fmt + lint + test
```

---

## Where to start when contributing

| You want to … | Open file |
|---|---|
| Add a new slash subcommand | `handlers/meta.py` — `on_slash_command` dispatch |
| Add a new Block Kit button | The relevant handler (status, toolbar, interactive, recovery), register the action |
| Support a new agent provider | `providers/registry.py` + new `AgentProvider` impl + hook adapter |
| Customise message formatting | `slack_formatting.py` (Block Kit) + `message_routing._decorate` (per-content-type) |
| Tune polling cadence | `handlers/polling/coordinator.py` |
| Add a new env knob | `config.py` + document in `docs/configuration.md` |
| Add a hook event type | `hook.py` + `handlers/hook_events.py` |
| Wire a new picker shape | `handlers/interactive.py` — `_build_blocks` + `_button_rows` |

Most additions live entirely in `handlers/`; the core (providers,
tmux, transcripts, hooks) is intentionally provider-agnostic and rarely
needs editing.

---

## What's intentionally not ported (yet)

See [`AGENT.md`](../AGENT.md) for the full list. High-level:

- Full shell pipeline (~1,400 LOC in ccgram — prompt markers, exit-code
  detection, NL→command, dangerous-command approval). Current ccslack
  has a minimal pre/post pane-diff capture.
- Inter-agent messaging "swarm".
- Mini App dashboard (Slack has no Telegram-WebApp equivalent).
- Voice messages + voice replies (modules ported, handlers not wired).
- Multi-pane prompt scanning (basic `/panes` listing exists; non-active-
  pane interactive UI alerts don't).
- Emdash integration (auto-discover externally-managed tmux sessions).

If you need any of these, open an issue / PR.
