# ccslack — Drive AI coding agents from Slack

[![Python 3.14+](https://img.shields.io/badge/python-3.14%2B-blue)](pyproject.toml)
[![License: MIT](https://img.shields.io/badge/license-MIT-green)](LICENSE)
[![ruff](https://img.shields.io/badge/lint-ruff-9333ea)](https://github.com/astral-sh/ruff)
[![pytest](https://img.shields.io/badge/tests-pytest-blue)](tests/)

**ccslack** bridges Slack and `tmux`-running AI coding agents. Each Slack
channel binds to one `tmux` window running one agent CLI — Claude Code,
Codex, Gemini, Pi, or a plain shell. Read your agent's output, click
permission prompts, drive arrow-key pickers, screenshot the terminal,
all from Slack.

Transplanted from [**ccgram**](https://github.com/alexei-led/ccgram) (the
Telegram bridge); ccslack ports the same agent-control surface to Slack
via Socket Mode. See [`AGENT.md`](AGENT.md) for the design rationale and
the per-feature Telegram → Slack mapping table.

---

## Features

| | What it does |
|---|---|
| 🆕 `/ccslack new` | Spawn a private session channel + tmux window in one click. Optional opt-in to a fresh `git worktree` |
| 🎛️ Toolbar | Provider-aware Block Kit buttons that drive the tmux TUI — arrows, Enter, Esc, Tab, Space, digits, Ctrl-C |
| 🔔 Live picker | On every `tool_use` of `AskUserQuestion` / `ExitPlanMode` / `request_user_input`, a Block Kit picker appears in the channel, refreshes as the pane changes, and resolves when the agent moves on. Fallback regex prober for non-hook providers (Codex `›`-arrow approval, shell prompts) |
| 👤 User echo | The user's own prompt echo is prefixed with a silhouette so you instantly see "what I said" vs "what the agent said" |
| 🔧 Tool-use chain | `tool_use` + matching `tool_result` are paired and edit the same Slack message in place — channel reads as one tool call → result chunk. Per-channel `shown` / `hidden` / `default` cycling |
| 📷 Screenshot | Viewport-only PNG of the pane, uploaded into the channel. Bounded size; focuses on recent ops |
| 📋 `/ccslack history` | Paginated transcript of the last N messages in the channel |
| ↩️ `/ccslack resume` | Block Kit list of past Claude sessions matching the channel's cwd |
| 🪟 `/ccslack panes` | Multi-pane window inspector |
| 📤 `/ccslack send` | Upload file(s) from the session's cwd — no-arg opens an interactive file browser, or pass a path/glob/substring. Project-scoped security filters (lexical path containment, secrets, gitleaks); files ≥10 MB ask for a one-tap confirm. Meta users can reach outside the cwd |
| 🛑 `/ccslack kill` | Kill the current session, a specific session, or `--all --confirm` everything |
| 💀 Recovery banner | When a window dies, a banner with Fresh / Continue / Resume / Archive buttons appears in the bound channel |
| 🔕 `/ccslack mute` | Per-channel notification mode: `all` / `errors` / `off` |
| ⚡ `/ccslack yolo [on\|off]` | Switch the running agent in/out of skip-approvals mode without losing context |
| 💬 `/ccslack chat` | Start a human-only thread in the channel — replies in it are not forwarded to the agent |
| 📊 Table render | When an agent answer contains a markdown table, offer a button to render it as a clean image (Slack renders tables poorly) |
| 👥 Channel-membership auth | Anyone you invite to a session channel can drive that session — no need to add every teammate to `ALLOWED_USERS` |

---

## Quick start

```bash
# 1. Install
git clone <repo>
cd ccslack
uv sync

# 2. Create a Slack app + private meta channel + .env
#    (see docs/setup.md for the walkthrough — or paste
#    slack-app-manifest.yaml into Slack's "From a manifest" flow
#    to get every scope + the slash command + Socket Mode in one shot)
cp .env.example ~/.ccslack/.env
$EDITOR ~/.ccslack/.env   # fill in SLACK_BOT_TOKEN, SLACK_APP_TOKEN,
                          #         SLACK_META_CHANNEL_ID, ALLOWED_USERS

# 3. Install hooks so Claude / Codex tell the bot what they're doing
uv run ccslack hook --install                          # Claude
uv run ccslack hook --install --provider codex         # Codex (optional)

# 4. Run
uv run ccslack
```

In your meta channel:

```
/ccslack new ~/code/my-project           # spawn a Claude session
/ccslack new ~/code/my-project codex     # …or Codex
/ccslack new                              # …or open the modal
```

A private channel `#ccslack-my-project` is created, the bot invites you,
the tmux window starts running the agent. Type into the channel; the
agent's reply streams back.

---

## Documentation

- 📦 [`docs/setup.md`](docs/setup.md) — Slack app + OAuth + tokens + first run
- 📋 [`slack-app-manifest.yaml`](slack-app-manifest.yaml) — copy-paste ready manifest for Slack's "From a manifest" flow
- 🎮 [`docs/commands.md`](docs/commands.md) — every slash command + Block Kit action
- 🔧 [`docs/configuration.md`](docs/configuration.md) — env vars, state files, per-channel settings
- 🏗️ [`docs/architecture.md`](docs/architecture.md) — module map + design decisions
- 🧪 [`docs/development.md`](docs/development.md) — tests, lint, contributing

For the Telegram parent project (covers the same conceptual model in a
different chat surface), see [ccgram](https://github.com/alexei-led/ccgram).
The internal port mapping lives in [`AGENT.md`](AGENT.md).

---

## Status

ccslack reached feature parity with the most-used ccgram surfaces over the
v0.0.1 development cycle. The deliberately-deferred items (shell prompt-
marker pipeline, inter-agent messaging "swarm", Mini App dashboard) are
documented in [`AGENT.md`](AGENT.md). 65 unit tests cover formatting,
sender, routing, picker, shell capture, prober, auth.

```bash
make check        # ruff fmt + lint + tests (default — fast)
make test-all     # everything except E2E
```

---

## Acknowledgements

ccslack is a near-direct port of [**ccgram**](https://github.com/alexei-led/ccgram)
by [Alexei Ledenev](https://github.com/alexei-led). The provider, transcript,
hook, and tmux subsystems are reused largely unchanged; the chat-transport
layer (Bolt, Block Kit, Socket Mode) is the only fundamentally new code.
Thanks to Alexei for the architecture that made this a one-week port.

## License

MIT
