# CLAUDE.md

Codebase guidance for assistant agents working in this repo.

For end-user docs see `README.md` and `docs/`. For the transplant design
rationale see `AGENT.md`.

---

## What this project is

ccslack drives AI coding agents (Claude Code, Codex, Gemini, Pi, plain
shell) from Slack via tmux. One Slack channel binds to one tmux window
running one agent CLI. Transplanted from
[ccgram](https://github.com/alexei-led/ccgram), which does the same
for Telegram.

Tech: Python 3.14, slack-bolt async, slack-sdk, libtmux, pyte, uv.

---

## Where things live

```
src/ccslack/
  config.py · session.py · thread_router.py    # core
  providers/ · llm/ · whisper/ · tts/          # provider-agnostic, ported
  tmux_manager.py · screen_buffer.py · …       # I/O
  slack_client.py · slack_formatting.py · slack_sender.py  # Slack transport
  bot.py · bootstrap.py · cli.py · main.py     # lifecycle + entry
  hook.py · hooks/                             # hook subprocess
  handlers/                                    # Slack-side handlers
    auth.py · registry.py
    meta.py · text.py · status.py · screenshot.py · toolbar.py
    interactive.py · new_modal.py · resume.py · history.py
    panes.py · send.py · send_security.py · recovery.py
    hook_events.py · shell_capture.py · worktree.py
    polling/ · messaging_pipeline/

tests/ccslack/                                 # unit tests (mirror src)
docs/                                          # user-facing docs
```

Architecture deep-dive: `docs/architecture.md`.

---

## Build + test commands

```bash
make check                  # fmt + lint + tests — run before commit
make fmt
make lint                   # MUST pass before commit
make typecheck              # nice-to-have
make test                   # unit tests only — fast (< 1 s)
make test-all               # everything except E2E (no E2E yet)
uv run ccslack              # start the bot
uv run ccslack status       # local state
uv run ccslack hook --install [--provider codex|gemini|pi]
```

---

## Core constraints

- **1 Channel = 1 Window = 1 Session.** Routing keyed by tmux
  `window_id` (`@7`, `@12`), not by name. Names are display labels.
- **Channel-membership = implicit auth** in bound session channels.
  Global `ALLOWED_USERS` only required for the meta channel and for
  cross-session actions (`/ccslack new`, dashboard kill, kill --all).
  See `handlers/auth.py`.
- **Block Kit, not MessageEntity.** Use the `slack_formatting` /
  `slack_sender` helpers; handlers depend on the `SlackClient` Protocol
  (`src/ccslack/slack_client.py`), not on `slack_sdk.AsyncWebClient`.
- **Socket Mode.** No HTTP webhook server, no public endpoint.
- **Live picker = JSONL tool_use-driven** (`AskUserQuestion`,
  `ExitPlanMode`, `request_user_input`) with a regex prompt-probe
  fallback for non-hook providers. See `handlers/interactive.py` +
  `handlers/polling/prompt_probe.py`.
- **Hook-based session tracking.** `ccslack hook` writes
  `~/.ccslack/{session_map.json, events.jsonl}`. SessionMonitor reads
  both. Hooks coexist alongside ccgram's hooks (each writes to its own
  state dir).
- **Pinned status message, not channel rename.** Slack rate-limits
  renames; ccslack pins one status message per channel and
  `chat.update`s it on state transitions.
- **Per-channel rate limit**: 1.1 s minimum between outbound messages
  (`slack_sender.rate_limit_send`).
- **Viewport-only screenshots** (`/screenshot` captures the visible
  pane, not scrollback — successive shots stay the same size).

---

## Code conventions

- Module docstrings: one-sentence first line + a short responsibilities
  list. Aim for clarity within 10 lines.
- Full variable names: `window_id`, `channel_id`, `session_id`,
  `tool_use_id`. Not `wid` / `cid` / `sid` / `tuid`.
- Catch specific exceptions (`OSError`, `ValueError`, `SlackApiError`);
  never bare `except Exception` in production paths.
- Tests: no comments, no docstrings on test functions — name is the
  spec. Use `FakeSlackClient` + monkeypatched env vars; don't reach for
  real Slack or real tmux.
- Lazy imports only when actually needed; annotate with
  `# Lazy: <reason>`.
- For new handlers: add a `register(app)` function and wire it via the
  loop in `handlers/registry.py`.

---

## Configuration

Precedence: CLI flag > env > `.env` (CWD > `$CCSLACK_DIR`) > default.

Required env: `SLACK_BOT_TOKEN`, `SLACK_APP_TOKEN`,
`SLACK_META_CHANNEL_ID`, `ALLOWED_USERS`.

Required Slack bot scopes (12+1): the 12 in the original manifest plus
`groups:history` (Slack rejects the `message.groups` event subscription
without it — `groups:read` only covers channel listing).

Full reference: `docs/configuration.md`.

State files in `$CCSLACK_DIR` (default `~/.ccslack`):

- `state.json` — channel bindings, window states, display names
- `session_map.json` — hook-written window → session map
- `events.jsonl` — hook event log
- `monitor_state.json` — per-session JSONL byte offsets

---

## Providers

Implementations in `src/ccslack/providers/`. Per-window resolution:
window's `provider_name` first, fall back to `CCSLACK_PROVIDER`
(default `claude`).

Capabilities gate UX per-window (`ProviderCapabilities`): hook event
types, resume / continue, transcript format, status detection, picker
hints. Each provider's hook payload is normalised by
`hooks/adapters.py`.

Launch overrides: `CCSLACK_<NAME>_COMMAND` (e.g.
`CCSLACK_CLAUDE_COMMAND=ce`).

Hook install paths:

- Claude: `~/.claude/settings.json` (managed by ccslack)
- Codex: `~/.codex/hooks.json` + `[features].hooks = true` in
  `~/.codex/config.toml` (managed by ccslack — auto-migrates the
  deprecated `codex_hooks` flag)
- Gemini: `~/.gemini/settings.json` (managed by ccslack)
- Pi: cc-thingz hook-runner (not managed by ccslack)

---

## Adding things

**New slash subcommand**: branch in `handlers/meta.py:on_slash_command`,
plus help text + docs entry. Auth via `is_authorized(user_id,
channel_id)` (channel-aware) or `is_meta_authorized(user_id)` (strict).

**New Block Kit action**: define a stable `action_id`, wire the handler
inside the relevant module's `register(app)`. Channel context lives in
`body.get("channel", {}).get("id", "")`.

**New env var**: declare in `config.Config.__init__`, document in
`docs/configuration.md` *and* `.env.example`.

**New provider**: subclass `AgentProvider` in `providers/<name>.py`,
register in `providers/registry.py`, add a launch-command branch in
`providers/__init__.py:resolve_launch_command`, add to
`handlers/meta._SUPPORTED_PROVIDERS` + the toolbar layout map.

---

## What's intentionally not yet ported

See `AGENT.md` for the full list with LOC estimates. Notably:

- Full shell pipeline (~1,400 LOC: prompt markers, exit-code detection,
  NL→command via LLM, dangerous-command approval). Current implementation
  is the minimal pre/post pane-diff in `handlers/shell_capture.py`.
- Inter-agent messaging "swarm".
- Mini App dashboard (Slack has no equivalent of Telegram WebApps).
- Voice messages + voice replies (modules ported, handlers not wired).

If a session-restart-time pain shows up, prioritise porting
`handlers/recovery/transcript_discovery.py` (~280 LOC) — gives Codex
hookless discovery parity with ccgram.

---

## Commit / contribution flow

Conventional Commits (`feat:`, `fix:`, `chore:`, `test:`, `refactor:`).
Sign with `Co-Authored-By:` when collaborating. Multi-line commit
messages explain *why*, not just *what*.

Before any commit:

```bash
make check
```

If something fails, don't commit until it passes. Don't use
`--no-verify` to bypass hooks; fix the underlying issue.
