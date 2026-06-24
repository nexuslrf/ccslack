# Multi-host (router + workers)

Drive sessions on **several machines from one Slack app and one meta channel**.
One process is the **router** (holds the single Slack connection); the others are
**workers** (run tmux + agents locally). The router forwards each Slack event to
the machine that owns the relevant channel; workers post back to Slack directly.

This is entirely opt-in. With no `CCSLACK_WORKERS` configured, `ccslack router`
behaves exactly like `ccslack run` — **standalone is unchanged**.

> **Single app = single intake.** A Slack app has exactly one Socket Mode
> intake, so a fleet and a separate standalone bot must use **different Slack
> apps**. You can keep a simple standalone deployment on its own app and run a
> fleet on another with zero interference.

---

## Why it's shaped this way

Slack Socket Mode delivers each event to **one** of an app's open connections,
chosen arbitrarily — there's no way to steer "channel X → connection 2". So a
fleet needs a **single intake** (the router). The asymmetry that makes this cheap:
only *inbound* (the event socket) is singular — *outbound* (`chat.postMessage`,
file uploads, reactions) is plain HTTPS with the bot token and works from every
machine at once. So workers post to Slack themselves; the router only fans
inbound events out.

```
 Slack ──(Socket Mode, app token)──► Router ──ack──► Slack
                                       │  route by channel_id (or --host)
        ┌──────────────────────────────┼───────────────────────┐
   (self / local)                  SSH tunnel               SSH tunnel
   Worker @ router-host          Worker @ gpu1             Worker @ gpu2
        └──────── each posts outbound to Slack directly (bot token) ──────┘
```

The router is also a **worker for its own host** (double role) — it runs sessions
locally too. Use `ccslack router` for a session-running router, or just don't
schedule sessions there.

---

## Requirements

- **One Slack app** (its `SLACK_APP_TOKEN` lives only on the router).
- **Outbound HTTPS to slack.com from every machine** (router *and* workers post
  directly). If a worker can't reach Slack, this layout doesn't apply.
- **Bidirectional SSH** from the router to each worker — a `~/.ssh/config` alias
  or `user@host` that `ssh <target>` resolves. Interactive auth (OTP) is fine;
  the operator answers it at the router console (see [Reconnects](#reconnects)).
- The **same checkout / install** of ccslack on every machine.

---

## Shared vs per-host config

It's one logical bot, so the Slack identity is shared; execution is per-host.

| Variable | Where |
|---|---|
| `SLACK_APP_TOKEN` | **Router only** (the single Socket Mode holder) |
| `SLACK_BOT_TOKEN`, `SLACK_META_CHANNEL_ID`, `ALLOWED_USERS`, `CCSLACK_SLASH_COMMAND`, `CCSLACK_PUBLIC_CHANNELS` | **Shared** — identical on every host |
| `CCSLACK_WORKERS` | **Router only** — the worker list |
| `CCSLACK_HOST`, `CCSLACK_LINK_PORT` | **Per-host** |
| `CCSLACK_PROVIDER`, tmux session, paths, channel prefix | **Per-host** (already local) |

New env vars (full reference in [configuration.md](configuration.md#multi-host-router--workers)):

| Variable | Default | Role | Meaning |
|---|---|---|---|
| `CCSLACK_HOST` | hostname | all | This machine's name in the fleet (shown in `list`, used by `--host`). |
| `CCSLACK_LINK_PORT` | `8765` | worker | Localhost port the worker's link server listens on (reached by the router over the tunnel). |
| `CCSLACK_WORKERS` | empty | router | Comma-separated `host=ssh_target` entries, e.g. `gpu1=user@gpu1,gpu2=gpu2-alias`. Empty = single-host router. |

---

## Set up a fleet

### 1. Pick a router host

It must hold the `SLACK_APP_TOKEN` and be able to `ssh` to every worker. It's
also a worker for its own host, so a session can run on it too.

### 2. Each worker host

Identical `.env` to the router **except no `SLACK_APP_TOKEN`** (a worker doesn't
open a socket), plus its own name:

```ini
# ~/.ccslack/.env  on gpu1
SLACK_BOT_TOKEN=xoxb-…            # shared
SLACK_META_CHANNEL_ID=C0…         # shared
ALLOWED_USERS=U0…,U0…             # shared
CCSLACK_HOST=gpu1
# CCSLACK_LINK_PORT=8765          # default; change only on a port clash
```

Start the worker (it stays up across SSH drops — the tunnel only carries the
link):

```bash
uv run ccslack worker            # serves the link on 127.0.0.1:8765
```

### 3. The router host

```ini
# ~/.ccslack/.env  on the router
SLACK_BOT_TOKEN=xoxb-…
SLACK_APP_TOKEN=xapp-…            # router only
SLACK_META_CHANNEL_ID=C0…
ALLOWED_USERS=U0…,U0…
CCSLACK_HOST=router0
CCSLACK_WORKERS=gpu1=user@gpu1, gpu2=gpu2-alias
```

```bash
uv run ccslack router
```

For each worker the router opens a supervised `ssh -N -L <localport>:127.0.0.1:8765 <target>`
tunnel, then speaks the link protocol over it. On connect each worker sends a
snapshot of the channels it owns; the router builds its `channel_id → host`
registry. You'll see **`:satellite: host \`gpu1\` connected.`** in the meta
channel.

> **Worker link port reachability.** The router connects to the worker's link
> server through the SSH tunnel (`127.0.0.1:8765` *on the worker*), so the port
> never needs to be exposed on the network — SSH is the only ingress a worker
> needs.

---

## Using a fleet

| Action | Routes by | Notes |
|---|---|---|
| `/ccslack new` (no args) | modal | The modal shows a **Host** dropdown — pick the target host, no `--host` needed. |
| `/ccslack new <dir> … --host gpu1` | `--host` | Creates the session on `gpu1`. Omit `--host` → the **router's own host**. A bad/disconnected `--host` is rejected with the available host list. |
| A message / button / `/ccslack kill`,`send`,`purge`,… in a session channel | `channel_id` | Goes to the owning host automatically — nothing to specify. |
| `/ccslack list` | — | Shows local sessions (detailed) **plus** a *remote* section: each remote channel and its host. |
| `/ccslack sessions` | — | Dashboard merges **all hosts'** sessions (gathered over the link), each tagged by host; the Kill button works cross-host. |
| `/ccslack fleet` | — | Per-host status: connected/disconnected, session count, ssh target. |

Everything else — purge, screenshots, the file browser, chat threads, `adduser`
grants — works per session channel and routes by `channel_id`, so it's
transparent across hosts.

---

## Reconnects & manual auth

Tunnels are supervised with keepalives and restart with backoff. The worker
process keeps running through a drop, so on reconnect it just re-attaches and
re-sends its channel snapshot — **sessions don't die**. (Host up/down lines are
silent by default; check `/ccslack fleet`, or set `CCSLACK_FLEET_NOTIFY=true`.)

**Answering SSH 2FA from Slack.** By default the `ssh` subprocess inherits the
router's stdio, so an OTP / passphrase prompt is answered at the router console.
Set **`CCSLACK_SSH_INTERACTIVE=true`** to instead run each tunnel under a PTY and
forward the auth prompt (e.g. Duo) to the meta channel: ccslack posts the prompt
with a **button per option** (`1 · Duo Push to …`) and a **Passcode…** modal, and
your click/answer is written back to the waiting `ssh`. Tune
`CCSLACK_SSH_PROMPT_RE` to your server's prompt wording. Answering is gated to
`ALLOWED_USERS`. Heads-up: this means an allow-listed user approves your SSH 2FA
from Slack.

---

## Failure semantics (be aware)

- **Worker link down:** events for that host's channels can't be delivered. The
  router doesn't ack those, so Slack **redelivers a few times** — covering a
  brief blip. If the worker stays down, Slack eventually gives up and the user's
  message is lost (they re-send). The disconnect notice tells you which host.
- **Router down:** no events at all (single intake). The workers keep running;
  bring the router back and it re-syncs from the workers. (Slack permits a
  standby router connection for HA — not wired yet.)
- These are reduce-exposure / best-effort properties, fine for a dev tool — not
  a delivery guarantee.

---

## Limits

- No standby/HA router (Slack allows a second connection for failover — not
  wired yet).
- SSH-2FA-over-Slack prompt detection is heuristic (`CCSLACK_SSH_PROMPT_RE`);
  tune it to your server.

---

## The simple alternative

For a handful of machines where a single shared meta channel isn't essential,
**one Slack app per machine** (separate tokens + meta channel) needs zero of
this — just N standalone bots. The router/worker model earns its keep
specifically when you want *one* shared meta channel across hosts.
