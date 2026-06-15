# Setup

Step-by-step from `git clone` to a bound session channel where you can drive
Claude / Codex from Slack.

---

## 1. Local prerequisites

- **Python 3.14+** (we use `uv` to manage the virtualenv but any Python that
  can install slack-bolt + slack-sdk works).
- **tmux** in `$PATH`. ccslack opens windows in whatever tmux session is in
  use; outside tmux it auto-creates one named `ccslack`.
- **At least one agent CLI** installed and signed in:
  - `claude` — recommended, has the richest hook surface
  - `codex` — works once you `ccslack hook --install --provider codex`
  - `gemini`, `pi` — supported, less battle-tested
  - or just `bash` / `zsh` for shell-mode sessions
- **uv** to install the project: `curl -LsSf https://astral.sh/uv/install.sh | sh`

---

## 2. Clone + install

```bash
git clone <ccslack repo>
cd ccslack
uv sync            # installs deps into .venv
uv sync --extra dev   # add dev deps (ruff, pytest, pyright)
```

Smoke test:

```bash
uv run ccslack --help        # CLI loads
uv run ccslack status        # local state inspector
make check                   # fmt + lint + tests
```

`make check` should pass before you continue.

---

## 3. Create the Slack app

Open <https://api.slack.com/apps> → **Create New App** → **From scratch**.

Name it `ccslack` (or whatever you like), pick the workspace.

### 3a. Enable Socket Mode

**Features → Socket Mode** → toggle **on**.

Generate an **app-level token** with the `connections:write` scope. Save
it; you'll need it as `SLACK_APP_TOKEN` (starts `xapp-`).

### 3b. OAuth scopes

**Features → OAuth & Permissions → Scopes → Bot Token Scopes**, add the
list below. The block at the end of this section is also a complete
manifest you can paste into **Features → App Manifest → YAML** to set
everything at once.

| Scope | Why ccslack needs it |
|---|---|
| `app_mentions:read` | Listen for `@ccslack` health pings |
| `chat:write` | Post messages, edit them via `chat.update`, send ephemerals |
| `commands` | Receive the `/ccslack` slash command |
| `channels:manage` | Create + archive session channels (when configured public) |
| `groups:read` | Basic info about private channels (list, lookup) |
| `groups:history` | **Required** for `message.groups` events to deliver bodies — without this Slack rejects the event subscription |
| `groups:write` | Create + archive private session channels |
| `groups:write.invites` | Invite the requesting user to the new session channel |
| `pins:write` | Pin the status message in each session channel |
| `pins:read` | Read existing pins (e.g. for restart-time recovery) |
| `files:write` | Upload screenshots, `/ccslack send` files |
| `files:read` | Read incoming file shares (currently passive) |
| `reactions:write` | ✓ / 🚫 / ⚠️ reactions on inbound prompts |

> **Heads-up**: in the Bot Scopes search box you'll see both `files:write`
> and `remote_files:write`. You want **`files:write`**. `remote_files:*`
> is for the Remote Files API and is unrelated.

### 3c. Event subscriptions

**Features → Event Subscriptions** → toggle **on**. Subscribe to bot events:

- `app_mention`
- `message.groups` (so the bot can see messages in private session channels)

> **Office / public-channel mode** (`CCSLACK_PUBLIC_CHANNELS=true`, for
> workspaces that forbid private channels): swap the `groups:*` scopes for
> `channels:manage` + `channels:history` + `channels:read`, and subscribe to
> `message.channels` instead of `message.groups`. Auth then requires
> `ALLOWED_USERS` + `/ccslack adduser` grants, and agent output is visible to
> the whole workspace. See [commands → Public (office) mode](commands.md#public-office-mode).

### 3d. Slash command

**Features → Slash Commands → Create New Command**:

- **Command**: `/ccslack` (or whatever you like — must match
  `CCSLACK_SLASH_COMMAND` if you set it)
- **Short description**: `Manage ccslack sessions`
- **Usage hint**: `new <dir> [provider]`
- **Request URL**: leave blank — Socket Mode delivers the command over
  the websocket, so no public endpoint is needed.

### 3e. Install to workspace

**Settings → Install App** → **Install to Workspace**. Approve the OAuth
prompt. Copy the **Bot User OAuth Token** (starts `xoxb-…`); you'll need
it as `SLACK_BOT_TOKEN`.

### 3f. App Manifest (one-shot, recommended)

There's a ready-to-paste manifest at the repo root:
[`slack-app-manifest.yaml`](../slack-app-manifest.yaml).

Two ways to use it:

**A) Brand-new app** (skips 3a–3e entirely):
1. <https://api.slack.com/apps> → **Create New App** → **From a manifest**.
2. Pick your workspace.
3. Paste the file's YAML, continue.
4. After creation: **Basic Information → App-Level Tokens → Generate
   Token and Scopes** → add `connections:write` → save → copy the
   `xapp-…` token (this is `SLACK_APP_TOKEN`).
5. **Install App → Install to Workspace** → copy the `xoxb-…` token
   (this is `SLACK_BOT_TOKEN`).

**B) Existing app** (sync scopes + slash command + event subscriptions):
1. Open your app in <https://api.slack.com/apps>.
2. **Features → App Manifest → YAML** → paste, save.
3. **Reinstall** the app (Slack prompts for it) to apply the new scopes.

Either way, the manifest sets:
- All 12 OAuth bot scopes ccslack needs
- The `/ccslack` slash command
- `app_mention` + `message.groups` event subscriptions
- Interactivity (needed for Block Kit buttons)
- Socket Mode enabled

Skip ahead to [§4](#4-create-the-meta-channel).

---

## 4. Create the meta channel

In Slack, create a **private channel** (e.g. `#ccslack`). Invite the bot:

```
/invite @ccslack
```

Open the channel → click the channel name → bottom of the panel → copy
the **Channel ID** (starts `C…`). This is your `SLACK_META_CHANNEL_ID`.

The meta channel is the "control plane": you'll create new sessions from
here, see the dashboard here, kill sessions from here.

---

## 5. Find your Slack user ID

Your profile picture → three dots (⋯) → **Copy member ID**. It looks
like `U0123ABC456`. This goes into `ALLOWED_USERS` (comma-separated if
multiple).

---

## 6. Configure ccslack

```bash
mkdir -p ~/.ccslack
cp .env.example ~/.ccslack/.env
$EDITOR ~/.ccslack/.env
```

Required:

```ini
SLACK_BOT_TOKEN=xoxb-…
SLACK_APP_TOKEN=xapp-…
SLACK_META_CHANNEL_ID=C0123ABC456
ALLOWED_USERS=U0123ABC456,U0987XYZ
```

Optional — see [`docs/configuration.md`](configuration.md) for the
complete env reference.

---

## 7. Install agent hooks

Hooks let the agents tell ccslack what they're doing (session start, tool
calls, stop, etc.). Without hooks ccslack still mostly works (it falls
back to transcript scanning for some providers) but with hooks the UX is
~1-2 s faster and more reliable.

```bash
uv run ccslack hook --install                          # Claude
uv run ccslack hook --install --provider codex         # Codex
uv run ccslack hook --install --provider gemini        # Gemini (if you use it)
```

Each call appends to the agent's settings (`~/.claude/settings.json`,
`~/.codex/hooks.json`, `~/.gemini/settings.json`) **alongside** any
existing entries — ccslack hooks coexist peacefully with ccgram or other
tools' hooks.

To inspect / uninstall:

```bash
uv run ccslack hook --status --provider codex
uv run ccslack hook --uninstall --provider codex
```

---

## 8. First run

```bash
uv run ccslack
```

You should see something like:

```
[info] Config initialized: dir=/home/you/.ccslack, ...
[info] Session monitor started
[info] Status polling started (interval=1.00s)
[info] Socket Mode connected; ccslack ready
```

### Verify

In your meta channel, type:

```
@ccslack hello
```

The bot should reply `:green_heart: ccslack online`. That's the
health-check loop — Slack → Socket Mode → handler → reply.

If that works, spawn your first session:

```
/ccslack new ~/code/my-project
```

ccslack will:
1. Create a new tmux window running `claude` (or your `CCSLACK_PROVIDER`)
2. Create a private channel `#ccslack-my-project`
3. Invite you
4. Pin a status message and post a welcome

Open the new channel and type a prompt. Within a second the agent's
reply arrives back in Slack with the 👤 prefix on your echo and 🤖
output below it.

---

## 9. Add teammates (optional)

Channel membership is implicit auth. To grant a teammate access to a
session, just invite them to its channel:

```
/invite @teammate
```

They can now drive that session — type prompts, press picker buttons,
take screenshots, run `/ccslack mute`/`history`/`kill` for that channel.
They **cannot** spawn new sessions or kill other people's sessions
unless you add their user ID to `ALLOWED_USERS` (then they can drive
the meta channel too).

---

## Troubleshooting

| Symptom | Fix |
|---|---|
| `@ccslack` doesn't reply at all | Bolt isn't connected. Check `xapp-` token, Socket Mode enabled, bot installed |
| `/ccslack new` silently fails | Missing `channels:manage` or `groups:write` scope — reinstall the app |
| 🚫 reaction on every message | Your user ID isn't in `ALLOWED_USERS` (or you're not a member of a bound channel) |
| No transcript ever streams back | Claude hook not installed — `uv run ccslack hook --install` |
| Codex Stop hook shows "invalid stop hook JSON output" | You're on an old ccslack; this was fixed by emitting `{}` on stdout. Pull latest. |
| Live picker doesn't appear on Codex approval prompts | Check `~/.ccslack/events.jsonl` for the SessionStart event — if missing, install Codex hooks |
| Slack rejects `name_taken` when creating a channel | A channel with that slug already exists; ccslack auto-retries with a `-@N` suffix |
| Channel rename fails / rate-limited | ccslack doesn't rename channels — it edits a pinned status message. If you're hitting limits, it's likely an unrelated app |

For deeper diagnostics, inspect `~/.ccslack/`:

```
state.json            # channel_bindings, window_states, display_names
session_map.json      # hook-written window→session map
events.jsonl          # append-only hook event log
monitor_state.json    # byte offsets the transcript reader has consumed
```
