# Commands

Every slash subcommand and Block Kit action ccslack ships. The slash command
itself defaults to `/ccslack`; you can rename it via `CCSLACK_SLASH_COMMAND`
(see [`configuration.md`](configuration.md)).

---

## Slash commands

### `/ccslack help`

Shows the full list of subcommands as an ephemeral message. Works in any
channel.

### `/ccslack new`

Create a new session.

| Form | Behaviour |
|---|---|
| `/ccslack new` | Opens a Block Kit modal — directory text input, provider radio, "create fresh git worktree" checkbox + optional branch name |
| `/ccslack new <dir>` | Default provider in `<dir>` |
| `/ccslack new <dir> <provider>` | `provider` ∈ `claude` `codex` `gemini` `pi` `shell` |
| `/ccslack new <dir> claude --worktree` | Spawns a fresh `git worktree` (auto-named `ccg/<slug>`) and uses *that* path as the session cwd |
| `/ccslack new <dir> claude --worktree feature-x` | Same but with a named branch |

**Where it works**: meta channel only.
**Auth**: `ALLOWED_USERS`.

What happens on success:

1. ccslack runs `tmux new-window` in `<dir>` (or the worktree path),
   launching the agent CLI.
2. A private Slack channel `#ccslack-<slug>` is created.
3. You're invited.
4. The channel topic + purpose carry the cwd and tmux window ID.
5. A status message is posted and pinned.
6. Welcome message + ephemeral confirmation in the meta channel.

### `/ccslack list`

Quick one-line-per-session text dump of every bound channel.

- **Where**: meta channel only.
- **Auth**: `ALLOWED_USERS`.

### `/ccslack sessions`

Interactive Block Kit dashboard. Each row: state emoji, channel mention,
provider, tmux window ID, display name, cwd, **🗑️ Kill button** with
confirm modal.

- **Where**: meta channel only.
- **Auth**: `ALLOWED_USERS`.
- **Kill button auth**: `ALLOWED_USERS` (meta-only action — channel
  members can't kill *other* people's sessions from here).

### `/ccslack history [N]`

Posts the last `N` (default 20, max 100) transcript messages as
ephemeral Block Kit context blocks. Useful for catching up on a session
without scrolling Slack.

Per-line emoji:

- 👤 `user`
- 🤖 `assistant`
- 💭 `thinking`
- 🔧 `tool_use`
- 🧾 `tool_result`

- **Where**: a bound session channel.
- **Auth**: channel membership.

### `/ccslack resume`

Scans `~/.claude/projects/*` for past Claude sessions whose cwd matches
the bound channel's cwd. Shows up to 6 as ephemeral buttons; clicking
one spawns a fresh tmux window running `claude --resume <id>` and
rebinds the channel.

- **Where**: a bound session channel.
- **Auth**: channel membership.
- **Limitation**: Claude only. Codex/Gemini/Pi have their own resume
  flows; tell us if you want them ported.

### `/ccslack restore [continue|resume|fresh]`

Recovers a session whose tmux window died — typically after a host
**reboot** or `tmux kill-server`. **Reuses the current channel** (never
creates a new one): rebuilds the tmux window, relaunches the agent, and
binds *this* channel to the new window.

| Mode | Behaviour |
|---|---|
| `continue` (default) | Relaunch and continue the latest session (`claude --continue`, `codex resume --last`, …). |
| `resume` | Relaunch with the remembered (or, for an unbound channel, the most-recent discovered) session id (`claude --resume <id>`, `codex resume <id>`); falls back to `continue` when no id is known. |
| `fresh` | Relaunch a clean session. |

Works in two situations:

- **Binding intact** — the channel still points at the (now-dead) window.
  Respawns from its remembered provider / cwd / session id. Refuses if the
  window is somehow still alive (use `/ccslack kill` first to start over).
- **Binding lost** — the channel is no longer bound (e.g. the bot's state
  was reset) but it's still a ccslack session channel. ccslack recovers the
  provider + cwd from the channel's own **topic** (which it wrote as
  `<provider> · <cwd>` at creation) and re-adopts the channel in place. If
  the topic isn't recognisable, restore declines and points you at
  `/ccslack new`.

For unattended recovery on every reboot, set
`CCSLACK_RESTORE_ON_START=continue` (or `resume`) instead — see
[configuration](configuration.md). (Auto-recovery only covers channels
whose binding survived; a lost binding needs a manual `/ccslack restore`
in the channel.)

- **Where**: a session channel (bound, or an unbound former session channel).
- **Auth**: channel membership (bound) / `ALLOWED_USERS` (unbound — the
  channel isn't a recognised member channel until re-adopted).

### `/ccslack panes`

Ephemeral list of every tmux pane in the bound window: active marker,
command, current path, dimensions. Useful when the agent is running a
multi-pane team.

- **Where**: a bound session channel.
- **Auth**: channel membership.

### `/ccslack send <path|glob|substring>`

Upload file(s) — including images, which Slack previews inline — from the
session's cwd to the channel. Three modes, auto-detected from the argument:

| Argument | Mode | Behaviour |
|---|---|---|
| `docs/arch.png` | exact path | Upload that file (relative to cwd; absolute paths must be inside cwd). |
| `*.png`, `report-??.csv` | glob | `fnmatch` over filenames in the cwd tree. |
| `arch` | substring | Case-insensitive filename search in the cwd tree. |

For glob / substring: the cwd is walked (depth-capped by
`CCSLACK_SEND_SEARCH_DEPTH`, excluded dirs like `node_modules` / `.venv`
pruned, capped at `CCSLACK_SEND_MAX_RESULTS`). **One** match uploads
immediately; **multiple** matches post an ephemeral picker — one button per
file (🖼️ for images, 📄 otherwise), plus an **Upload all N** button when the
count is ≤10.

Security filters (all enforced on every upload — direct, picked, or bulk;
deny-by-default):
- Path containment (resolved path must stay inside cwd; blocks `../`
  and symlink escapes)
- Hidden files / directories (`.`-prefixed) denied
- Secret-name patterns (`*.pem`, `*.key`, `*.env`, `*credential*`,
  `*secret*`, …)
- `.gitignore` and `.gitleaks.toml` rules
- 50 MB Slack file cap

- **Where**: a bound session channel.
- **Auth**: channel membership (the picker buttons re-check on click).
- **Tunables**: `CCSLACK_SEND_SEARCH_DEPTH` (5), `CCSLACK_SEND_MAX_RESULTS`
  (50) — see [configuration](configuration.md).

### `/ccslack mute [all|errors|off]`

Per-channel notification mode.

| Mode | Effect |
|---|---|
| `all` (default) | Every transcript message posts |
| `errors` (alias `errors_only`) | Only error-like content + tool flows post |
| `off` (alias `muted`) | Plain text suppressed; tool flows still post so the agent can progress |

No arg cycles through the three modes.

- **Where**: a bound session channel.
- **Auth**: channel membership.

### `/ccslack toolcalls [shown|hidden|default]`

Per-channel tool-use / tool-result visibility.

| Mode | Effect |
|---|---|
| `shown` | Always show the tool chain in this channel |
| `hidden` | Always hide tool calls |
| `default` | Defer to the global `CCSLACK_HIDE_TOOL_CALLS` env var |

No arg cycles. Default global is `shown` (matches ccgram).

- **Where**: a bound session channel.
- **Auth**: channel membership.

### `/ccslack thread [on|off|default]`

Per-channel grouping of a turn's tool chain into a Slack thread.

| Mode | Effect |
|---|---|
| `on` | A turn's `tool_use` / `tool_result` / `thinking` are posted under one threaded parent in the main channel; plain answers + interactive prompts stay in the main channel. |
| `off` | Tool calls post flat in the channel (no thread). |
| `default` | Defer to the global `CCSLACK_THREAD_TOOL_CALLS` env var (default `true`). |

No arg cycles. The thread parent shows `🛠️ Tool activity — running…` while
the turn is in progress and is rewritten to `🛠️ N tool calls · done` when the
turn ends (agent Stop hook, or the agent's final answer, or the next user
message). Tool-use → tool-result pairing still edits the original message in
place — inside the thread.

- **Where**: a bound session channel.
- **Auth**: channel membership.

### `/ccslack kill [target | --all --confirm]`

Tear down sessions.

| Form | Behaviour |
|---|---|
| `/ccslack kill` (from a session channel) | Kills *this* channel's session |
| `/ccslack kill <#channel>` (from meta) | Kills the session bound to the mentioned channel |
| `/ccslack kill C0123ABC` (from meta) | Same but by raw channel ID |
| `/ccslack kill @14` (from meta) | Same but by tmux window ID |
| `/ccslack kill --all` (from meta) | Dry-run — reports how many sessions would be killed |
| `/ccslack kill --all --confirm` (from meta) | Actually kills everything |

Each kill does (in order):

1. Remove the pinned status message
2. `tmux kill-window`
3. Unbind the channel from the router
4. Drop the `WindowState`
5. Forget polling bookkeeping
6. `conversations.archive` the Slack channel

- **Where**: session channel (no-arg form) OR meta channel (any form).
- **Auth**: session-channel form requires channel membership; targeted +
  `--all` forms require `ALLOWED_USERS`.

---

## Block Kit actions

### Status-message buttons (pinned in each session channel)

| Button | Action |
|---|---|
| 📷 **Screenshot** | Captures the visible viewport of the tmux pane, renders to PNG, uploads via `files.upload_v2`. Bounded size — focuses on most recent operations. |
| 🎛️ **Toolbar** | Posts a separate Block Kit toolbar message with per-provider key buttons (see below). |
| 🗑️ **Archive** | Confirm modal → kills the tmux window, unbinds the channel, archives the Slack channel. |

### Toolbar (posted by 🎛️)

Provider-specific layouts:

| Provider | Layout |
|---|---|
| **Claude** | Esc · Shift-Tab (Mode) · Tab (Think) · Ctrl-C ··· ↑ ↓ Enter Bksp ··· 1 2 3 4 5 |
| **Codex** | Esc · Tab · Shift-Tab (Model) · Ctrl-C ··· ↑ ↓ Enter Bksp ··· 1 2 3 4 5 |
| **Gemini** | same as Codex |
| **Pi** | same as Claude |
| **Shell** | Enter · Ctrl-C · Ctrl-D · Ctrl-Z ··· ↑ ↓ Tab Esc |

Every button has `action_id="ccslack_key:<tmux-key>"` and routes to
`tmux_manager.send_keys(window_id, key, literal=False, enter=False)`.
A separate ✖ **Close** button deletes the toolbar message.

### Live picker (posted on interactive prompts)

Triggered automatically when a `tool_use` of `AskUserQuestion` /
`ExitPlanMode` / `request_user_input` appears in the JSONL transcript,
**or** when the fallback regex prober detects a TUI selector in the
pane (Codex `›`-arrow + numbered list, inline `[y/N]`).

Buttons:
- ↑ ↓ ← → (arrows)
- ⏎ Enter (primary)  ⎋ Esc  ⇥ Tab  ␣ Space  ⌫ Bksp
- 1 2 3 4 5 (picker digits)
- 🗑️ Dismiss (closes the picker locally without sending keys)

The picker re-edits itself every 0.8 s as the pane changes. It auto-
closes when:
- the matching `tool_result` arrives
- the agent's Stop hook fires
- the tmux window dies
- no pane change for 60 s
- the user clicks Dismiss

On close, the picker message is **deleted** (matches ccgram — no
terminal-state stub).

### Recovery banner (posted when a window dies)

Appears in the channel within ~1 s of polling detecting a dead window.

| Button | Behaviour |
|---|---|
| ✨ **Fresh** | New tmux window, same provider + cwd, fresh agent session |
| 🔄 **Continue** | Same as Fresh + `--continue` flag (provider-dependent) |
| ⏪ **Resume** | Same as Fresh + `--resume <last_session_id>` (Claude only) |
| 🗑️ **Archive** | Kill + archive |

Fresh / Continue / Resume rebind the channel and re-post the status
message; Archive removes everything.

---

## CLI commands (run from your terminal)

| Command | What it does |
|---|---|
| `ccslack` (or `ccslack run`) | Start the bot |
| `ccslack hook --install [--provider X]` | Install agent hooks |
| `ccslack hook --uninstall [--provider X]` | Remove ccslack's hook entries |
| `ccslack hook --status [--provider X]` | Inspect installed hooks |
| `ccslack status` | Show local ccslack state (config dir, paths, tmux session name) |
| `ccslack doctor [--fix]` | Validate setup (stub today) |
| `ccslack --help` | CLI help |
| `ccslack --version` | Version string |

The `ccslack hook` command without a flag is also the entry point Slack /
Codex spawn as a subprocess to write hook events; you typically don't
invoke that form by hand.

---

## Permission summary

| Action | Permission |
|---|---|
| `/ccslack new` (modal or CLI form) | `ALLOWED_USERS` |
| `/ccslack list`, `/ccslack sessions` | `ALLOWED_USERS` |
| Dashboard 🗑️ Kill button | `ALLOWED_USERS` |
| `/ccslack kill --all`, kill by `<#channel>` / `C…` / `@N` | `ALLOWED_USERS` |
| `/ccslack kill` (from session channel) | Channel membership |
| `/ccslack mute`, `history`, `resume`, `restore`, `panes`, `send`, `toolcalls`, `thread` | Channel membership |
| Inbound message → tmux | Channel membership |
| Status-message buttons (Screenshot, Toolbar, Archive) | Channel membership |
| Live picker buttons | Channel membership |
| Recovery banner buttons | Channel membership |
| Toolbar key buttons | Channel membership |
| `@ccslack` mention | `ALLOWED_USERS` (in meta) OR channel membership (in session channel) |

The principle: meta channel + cross-cutting actions ("create a session",
"kill someone else's", "kill everything") require the global allow-list;
in-channel actions defer to who Slack let into the channel.
