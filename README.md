# ccslack

Manage AI coding agents from Slack via tmux. Each Slack channel binds to one tmux window running one agent CLI (Claude Code, Codex, Gemini, Pi, or shell).

Transplanted from [ccgram](https://github.com/alexei-led/ccgram); ccgram runs on Telegram, ccslack runs on Slack via Socket Mode.

## Status

Walking skeleton — under active construction. See `AGENT.md` for design choices and progress.

## Quick start

```bash
uv sync
cp .env.example ~/.ccslack/.env
# fill in SLACK_BOT_TOKEN, SLACK_APP_TOKEN, SLACK_META_CHANNEL_ID, ALLOWED_USERS
ccslack
```

## Slack app setup

1. Create a Slack app at <https://api.slack.com/apps> → "From scratch".
2. **Socket Mode**: enable; generate an App-Level Token with `connections:write` scope → use as `SLACK_APP_TOKEN`.
3. **OAuth & Permissions** → bot scopes:
   - `channels:manage`, `channels:read`, `channels:write.invites`
   - `groups:write`, `groups:read`, `groups:write.invites`
   - `chat:write`, `chat:write.public`
   - `commands`
   - `files:write`, `files:read`
   - `pins:write`, `pins:read`
   - `users:read`
4. **Slash commands**: register `/ccslack` (any description).
5. **Event subscriptions** → enable; subscribe to `message.channels`, `message.groups`, `app_mention`.
6. Install to workspace; copy bot token (`xoxb-...`) into `SLACK_BOT_TOKEN`.
7. Create a private meta channel (e.g. `#ccslack`), invite the bot, put its channel ID into `SLACK_META_CHANNEL_ID`.
