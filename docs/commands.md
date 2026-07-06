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
| `/ccslack new` | Opens a Block Kit modal — directory text input, provider radio, "create fresh git worktree" + "YOLO" checkboxes + optional branch name |
| `/ccslack new <dir>` | Default provider in `<dir>` |
| `/ccslack new <dir> <provider>` | `provider` ∈ `claude` `codex` `gemini` `pi` `shell` `cursor` |
| `/ccslack new <dir> claude --worktree` | Spawns a fresh `git worktree` (auto-named `ccg/<slug>`) and uses *that* path as the session cwd |
| `/ccslack new <dir> claude --worktree feature-x` | Same but with a named branch |
| `/ccslack new <dir> codex --yolo` | Launches the agent with approvals/sandbox **skipped** — see YOLO below |
| `/ccslack new <dir> --host gpu1` | Runs the session on a specific fleet host (multi-host router) — see below |

#### `--host` (multi-host)

In a [multi-host fleet](multi-host.md), `--host <name>` runs the session on a
specific worker; omit it to use the router's own host. A name that isn't a
connected host is rejected with the available host list. No-op (single host)
without a router. The no-arg modal form always targets the router host.

#### YOLO (permissive launch)

`--yolo` (alias `--dangerous`, or the modal checkbox) starts the agent with
its skip-approvals flag so it edits files and runs commands without asking:

| Provider | Flag appended |
|---|---|
| `claude` | `--dangerously-skip-permissions` |
| `codex` | `--dangerously-bypass-approvals-and-sandbox` |
| `gemini` | `--yolo` |
| `cursor` | `--force` |

It's a no-op for `pi` / `shell` (no such mode) — the request is ignored with
an ephemeral warning. YOLO sessions are flagged with a :warning: in the
start announcement and the channel's welcome message. Combine freely with
`--worktree` to keep an autonomous agent boxed into a throwaway branch.

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
7. A **Join** notice is posted in the meta channel mentioning the *other*
   `ALLOWED_USERS` — any of them can click **📥 Join session** to be invited
   into the new private channel. Skipped when there are no other allowed
   users, or when `CCSLACK_JOIN_OFFER=false` (see
   [configuration](configuration.md)).

### `/ccslack list`

Quick one-line-per-session text dump of every bound channel. In a
[multi-host fleet](multi-host.md) it adds a *remote* section: each channel owned
by another host, with that host's name.

- **Where**: meta channel only.
- **Auth**: `ALLOWED_USERS`.

### `/ccslack fleet`

Multi-host only: per-host status — each configured host with a connected/
disconnected dot, session count, and ssh target. See
[multi-host.md](multi-host.md).

- **Where**: meta channel only.
- **Auth**: `ALLOWED_USERS`.

### `/ccslack sessions`

Interactive Block Kit dashboard. Each row: state emoji, channel mention,
provider, tmux window ID, display name, cwd, **🗑️ Kill button** with
confirm modal. In a [multi-host fleet](multi-host.md) it merges every host's
sessions (each remote row tagged by host), and the Kill button works cross-host.

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

### `/ccslack rename <new-name>`

Renames the Slack channel you run it from. The name is lowercased and
sanitised to a Slack-legal slug (alphanumeric + hyphen, ≤60 chars), so
`Auth Refactor` becomes `auth-refactor`. The `channel_id` is stable across
renames, so all bindings keep working.

- **Where**: a bound session channel (it renames *that* channel).
- **Auth**: channel membership.
- If the target name is already taken Slack returns `name_taken` and the
  command reports it without changing anything.

### `/ccslack send [path|glob|substring]`

Upload file(s) — including images, which Slack previews inline — from the
session's cwd to the channel. Four modes, auto-detected from the argument:

| Argument | Mode | Behaviour |
|---|---|---|
| *(none)* | **browser** | Opens an interactive file browser rooted at the cwd — tap a 📁 folder to descend, a file to send it, ✖ **Close** to dismiss. Navigation is contained to the cwd. |
| `docs/arch.png` | exact path | Upload that file (relative to cwd; absolute paths must be inside cwd unless you're meta-authorized — see below). |
| `*.png`, `report-??.csv` | glob | `fnmatch` over filenames in the cwd tree. |
| `arch` | substring | Case-insensitive filename search in the cwd tree. |

The browser can also be opened from the **📤 File** button on the pinned
status message. It navigates in place (the ephemeral is replaced, not stacked)
and pages at 40 entries per folder.

For glob / substring: the cwd is walked (depth-capped by
`CCSLACK_SEND_SEARCH_DEPTH`, excluded dirs like `node_modules` / `.venv`
pruned, capped at `CCSLACK_SEND_MAX_RESULTS`). **One** match uploads
immediately; **multiple** matches post an ephemeral picker — one button per
file (🖼️ for images, 📄 otherwise), plus an **Upload all N** button when the
count is ≤10. An **exact path you name** (e.g. `build/out.bin`) is always
honoured even under a pruned dir — pruning only limits *search*.

**Large-file confirm**: files **≥ 10 MB** prompt a `:inbox_tray: Upload (X MB)`
/ `Cancel` button instead of uploading immediately; smaller files upload
straight away. (Bulk **Upload all** skips the per-file confirm — it's an
explicit opt-in.)

Security filters (all enforced on every upload — direct, picked, or bulk;
deny-by-default):
- Path containment — **lexical**: the path must sit under the cwd *by name*.
  `../` traversal is blocked, but a **symlinked directory under the cwd is
  followed** (projects intentionally link in data/output dirs), so it's
  navigable in the browser and sendable.
- Hidden files / directories (`.`-prefixed) denied
- Secret-name patterns (`*.pem`, `*.key`, `*.env`, `*credential*`,
  `*secret*`, …)
- `.gitleaks.toml` rules
- No hard size cap — files **≥ 10 MB** require a one-tap confirm (see above);
  there's no upper limit beyond what Slack itself accepts.

> **Gitignored files are allowed.** Build artifacts, logs, datasets, and
> model checkpoints are routinely gitignored yet legitimately worth sending.
> Secrets remain blocked by the hidden-file, secret-pattern, and gitleaks
> checks — those don't depend on a file being tracked by git.

> **Outside the cwd (meta users).** Members of the global allow-list
> (`ALLOWED_USERS`) may retrieve files from *anywhere*: the containment +
> hidden checks are skipped for them and the browser roots at the filesystem,
> so it can navigate above the cwd. The secret-pattern and gitleaks guards
> still apply. Regular channel members stay confined to the cwd.

- **Where**: a bound session channel.
- **Auth**: channel membership (the picker buttons re-check on click);
  outside-cwd access additionally requires `ALLOWED_USERS`.
- **Tunables**: `CCSLACK_SEND_SEARCH_DEPTH` (5), `CCSLACK_SEND_MAX_RESULTS`
  (50) — see [configuration](configuration.md).

### `/ccslack mute [all|errors|off|silent]`

Per-channel notification mode. Controls what the session posts *back* to Slack;
your input always still forwards **into** the tmux session.

| Mode | Effect |
|---|---|
| `all` (default) | Every transcript message posts |
| `errors` (alias `errors_only`) | Only error-like content + tool flows post |
| `off` (alias `muted`) | Plain text suppressed; tool flows still post so the agent can progress |
| `silent` (aliases `quiet` `none` `deaf`) | **Nothing** posts back — not text, tool flows, or the live picker; only the status pill updates. Send commands in, watch execution via [`/toolbar`](#-toolbar) + [`/screenshot`](#-screenshot). |

No arg cycles through the four modes (`all → errors → off → silent → all`).

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

### `/ccslack yolo [on|off]`

Switch the **running** agent between normal and YOLO (skip-approvals) mode
without losing context — distinct from `/ccslack new --yolo`, which only sets
the mode at creation.

| Form | Behaviour |
|---|---|
| `/ccslack yolo` / `yolo on` | Restart in YOLO (approvals/sandbox skipped). |
| `/ccslack yolo off` (alias `normal`) | Restart with approvals required again. |

Posts a confirm message; on click it Ctrl-C's the agent until the pane is
back at a shell, then relaunches it with the target mode's launch flags plus
`--continue` (so the conversation resumes). If the agent ignores repeated
Ctrl-C the switch is aborted with a hint to `kill` + `restore`. Switching
*to* YOLO needs a YOLO-capable provider (claude/codex/gemini/cursor); switching
to normal works for any provider.

### `/ccslack relaunch [--fresh] [args…]`

Restart the **running** agent with *arbitrary* custom CLI args — the flexible
cousin of `yolo` (which only toggles the fixed skip-approvals flag). Everything
after `relaunch` is passed straight to the agent's launch command.

| Form | Result (provider `claude`) |
|---|---|
| `/ccslack relaunch --model opus` | `claude --continue --model opus` |
| `/ccslack relaunch --fresh --model opus` | `claude --model opus` (new session) |
| `/ccslack relaunch --append-system-prompt "be terse"` | `claude --continue --append-system-prompt 'be terse'` |

Same mechanics as `yolo`: posts a confirm with the exact command, then on click
Ctrl-C's the agent back to a shell and relaunches it. The session **continues**
by default (`--fresh` starts clean). Custom args are `shlex`-quoted, so
multi-word values survive and shell metacharacters become literal arguments to
the agent (never the shell). Works for any provider.

- **Where**: a bound session channel.
- **Auth**: channel membership.

- **Where**: a bound session channel.
- **Auth**: channel membership.

### `/ccslack chat [topic]`

Start a **human-only** thread for the team to talk in without typing into the
agent. Posts a parent message (optionally seeded with `topic`) and marks its
thread — any reply underneath is **not** forwarded to tmux. The marker is
persisted, so it survives a bot restart. Messages outside the thread still
reach the agent as usual.

- **Where**: a bound session channel.
- **Auth**: channel membership.

### `/ccslack here <dir> [provider]`

Bind **the current channel** to a fresh tmux session — the bring-your-own-channel
path for when the bot can't (or shouldn't) create the channel itself. You create
the channel, add the ccslack bot, then run this inside it. Spawns the window,
binds this channel, pins the status message, and posts a welcome. Refuses if the
channel is already a session.

- **Where**: a channel the bot is in, not already bound.
- **Auth**: `ALLOWED_USERS` (binding a new session is a privileged action).

### `/ccslack adduser @user …` · `removeuser @user …` · `users`

Manage **who may drive this session** — relevant in [public mode](#public-office-mode),
where channel membership alone isn't trusted. `adduser`/`removeuser` grant or
revoke access **scoped to this channel only** (persisted, survives restart and
restore); `users` lists current grants. `ALLOWED_USERS` always have access on top
of any grants.

- **Where**: a bound session channel.
- **Auth**: `adduser`/`removeuser` require `ALLOWED_USERS`; `users` is readable by
  anyone already authorized in the channel.

### `/ccslack purge [N | all | since <dur>]`

Delete **ccslack's own output** from the channel — for tidying up or cutting
lingering exposure in a public channel.

| Form | Behaviour |
|---|---|
| `/ccslack purge` / `purge all` | Delete all recorded output in this channel. |
| `/ccslack purge 20` | Delete the most recent 20 messages. |
| `/ccslack purge since 30m` | Delete output posted in the last `30m` / `2h` / `1d`. |

Only messages **ccslack posted** (agent answers, tool chains, thinking, prompt
echoes, shell command output, and **uploaded files** — screenshots / `send`)
are deleted — never your typed prompts (Slack only lets a bot delete its own
messages), the pinned **status message**, or **`/ccslack chat`** threads.
Uploaded files are removed via `files.delete`, so the underlying file object
goes too — not just the message.

- **Where**: a bound session channel.
- **Auth**: channel membership.

### `/ccslack autopurge [off | Xh]`

Auto-delete this channel's output once it's older than a window. `X` may be a
float, with `s`/`m`/`h`/`d` units (bare number = hours). No arg reports the
current setting.

| Form | Behaviour |
|---|---|
| `/ccslack autopurge 1.5h` | Delete output older than 1.5 hours (swept every ~5 min). |
| `/ccslack autopurge 30m` | …older than 30 minutes. |
| `/ccslack autopurge off` | Disable (default). |

Persisted per-channel (survives restart). Same exclusions as `purge`.

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
| 📤 **File** | Opens the interactive file browser (same as a no-arg `/ccslack send`) as an ephemeral for the clicker. |
| 🗑️ **Archive** | Confirm modal → kills the tmux window, unbinds the channel, archives the Slack channel. |

These buttons resolve the target window from the channel's **current** binding
(not the id baked into the message), so they keep working after a `/ccslack
restore` rebinds the channel to a new tmux window.

### Toolbar (posted by 🎛️)

Provider-specific layouts:

| Provider | Layout |
|---|---|
| **Claude** | Esc · Shift-Tab (Mode) · Tab (Think) · Ctrl-C ··· ↑ ↓ Enter Bksp ··· 1 2 3 4 5 |
| **Codex** | Esc · Tab · Shift-Tab (Model) · Ctrl-C ··· ↑ ↓ Enter Bksp ··· 1 2 3 4 5 |
| **Gemini** | same as Codex |
| **Pi** | same as Claude |
| **Shell** | Enter · Ctrl-C · Ctrl-D · Ctrl-Z ··· ↑ ↓ Tab Esc |

The toolbar message also shows the **last few lines of live tmux pane text**
above the buttons. A background task re-captures the pane every second and
edits the message in place whenever the content changes, so you can watch the
TUI react as you press keys. The refresh runs until you close the toolbar (or
the window dies).

Every button has `action_id="ccslack_key:<tmux-key>"` and routes to
`tmux_manager.send_keys(window_id, key, literal=False, enter=False)`.
A separate ✖ **Close** button deletes the toolbar message and stops its
live-text refresh.

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

### File browser (posted by `/ccslack send` with no arg, or the 📤 File button)

An ephemeral, in-place file browser rooted at the session cwd:

- 📁 **folder** buttons descend into a directory (the ephemeral is replaced
  via `response_url`, so navigation doesn't stack); ⬆️ **..** goes up.
- file buttons (🖼️ images / 📄 otherwise) send that file through the same
  upload + security path as `/ccslack send <path>`.
- ✖ **Close** dismisses the browser.

Contained to the cwd (symlinked dirs under it are followed); meta-authorized
users can navigate above the cwd. See `/ccslack send` for the full security
model.

### Table-render offer (posted under an agent answer containing a table)

Slack renders markdown tables poorly. When a plain agent answer contains a
GitHub-flavored table, the raw text is posted unchanged and a follow-up prompt
offers **🖼️ Render image** / **✖ Dismiss**. Render lays the table(s) out as an
aligned box and uploads a PNG. Controlled globally by `CCSLACK_TABLE_RENDER`
(default on) — see [configuration](configuration.md).

### Tool-thread Close button

Every tool-chain thread parent (the `🛠️ Tool activity` message) carries a
**🗑️ Close** button that deletes the whole thread — the parent and all its
tool/thinking replies — in one click.

### Remove-file button

Every uploaded file — a `/screenshot` PNG, a `/ccslack send` file, or a
rendered-table image — is followed by a **🗑️ Remove** button. Clicking it
deletes the file via `files.delete` (removing it from the channel entirely, not
just hiding the message) along with the button. `/ccslack purge` and
`autopurge` also remove these files.

### Per-response purge button (public channels)

In [public mode](#public-office-mode), each round gets **one** **🗑️ Purge**
button, posted just **before** the round's responses (not one per message).
Clicking it deletes that whole round's output and the button. Your prompt echo
is **kept** — it's edited in place with a "_Responses purged._" line so the
channel still shows what was asked. A quick way to wipe one exchange without
running `/ccslack purge`; see that for the bulk/auto forms.

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

## Public (office) mode

For workspaces that **forbid private channels** (so the `groups:write` family
is unavailable), set `CCSLACK_PUBLIC_CHANNELS=true`. This flips two coupled
behaviours:

1. **Session channels are public** — created via `channels:manage` (or, if the
   bot can't create them, by you, then bound with `/ccslack here`).
2. **Channel membership is no longer trusted.** A public channel anyone can
   join must not grant terminal access, so auth becomes **`ALLOWED_USERS` +
   per-channel `/ccslack adduser` grants** for *all* in-channel actions
   (messages → tmux, buttons, everything). The two are coupled on purpose:
   public + member-trust would let any workspace member drive your terminal.

Setup deltas vs the default manifest:
- Scopes: `channels:manage`, `channels:history`, `channels:read` (in place of
  the `groups:*` create/read/history).
- Event subscription: `message.channels` instead of `message.groups`.
- Any channel-management call the bot isn't allowed to make (create / invite /
  topic / archive / rename) degrades to an **instruction message** instead of a
  hard failure — e.g. create-denied points you at `/ccslack here`.

> ⚠️ **Confidentiality:** in a public channel, the agent's terminal output,
> `/ccslack send` uploads, and screenshots are visible to the **whole
> workspace**. The `/send` secret/gitleaks filters still apply to *files*, but
> can't stop the agent from echoing a secret into the transcript. Don't run
> secret-bearing sessions this way.

To cut *lingering* exposure, public mode adds a **🗑️ Purge** button before
each answer, and you can `/ccslack purge` on demand or
`/ccslack autopurge Xh` to auto-delete output after X hours. Note these reduce
casual visibility but aren't a confidentiality guarantee — Slack retains
content server-side (eDiscovery/exports), and anyone already looking has seen
it.

---

## Permission summary

| Action | Permission |
|---|---|
| `/ccslack new` (modal or CLI form) | `ALLOWED_USERS` |
| `/ccslack list`, `/ccslack sessions` | `ALLOWED_USERS` |
| Dashboard 🗑️ Kill button | `ALLOWED_USERS` |
| `/ccslack kill --all`, kill by `<#channel>` / `C…` / `@N` | `ALLOWED_USERS` |
| `/ccslack kill` (from session channel) | Channel membership |
| `/ccslack mute`, `history`, `resume`, `restore`, `panes`, `send`, `rename`, `toolcalls`, `thread`, `yolo`, `relaunch`, `chat`, `users`, `purge`, `autopurge` | Channel membership* |
| `/ccslack here` (bind current channel) | `ALLOWED_USERS` |
| `/ccslack adduser`, `removeuser` | `ALLOWED_USERS` |
| `/ccslack send` outside the cwd | `ALLOWED_USERS` (on top of channel membership) |
| Inbound message → tmux | Channel membership* (chat-thread replies are never forwarded) |
| Status-message buttons (Screenshot, Toolbar, File, Archive) | Channel membership |
| File-browser + table-render buttons | Channel membership |
| Live picker buttons | Channel membership |
| Recovery banner buttons | Channel membership |
| Toolbar key buttons | Channel membership |
| `@ccslack` mention | `ALLOWED_USERS` (in meta) OR channel membership (in session channel) |

\* **In [public mode](#public-office-mode)** (`CCSLACK_PUBLIC_CHANNELS=true`),
"channel membership" no longer grants access — those rows require `ALLOWED_USERS`
or an explicit `/ccslack adduser` grant for that channel instead.

The principle: meta channel + cross-cutting actions ("create a session",
"kill someone else's", "kill everything") require the global allow-list;
in-channel actions defer to who Slack let into the channel (private mode) or to
explicit grants (public mode).
