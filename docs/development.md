# Development

For working on ccslack itself.

---

## Toolchain

| Tool | Purpose |
|---|---|
| **uv** | Virtualenv + dependency management |
| **ruff** | Formatter + linter |
| **pyright** | Static type checking |
| **pytest** | Tests |
| **hatchling** | Build backend |

All managed inside the `.venv` `uv sync --extra dev` creates. No global installs needed.

---

## Day-to-day commands

```bash
make fmt           # ruff format
make lint          # ruff check
make typecheck     # pyright src/ tests/
make test          # unit tests only — fast
make test-all      # everything except E2E (none present yet)
make check         # fmt + lint + test (run before committing)
make install       # uv sync — reinstall dependencies
make dev           # uv sync --extra dev
make build         # uv build (wheel + sdist)
make clean         # remove caches
```

`make check` is the gate. If it doesn't pass, neither does CI (when CI lands).

---

## Project layout reminder

```
src/ccslack/
  config.py · session.py · thread_router.py · …  # core
  providers/ · llm/ · whisper/ · tts/             # provider-agnostic
  handlers/                                       # Slack-side handlers
    meta.py · text.py · status.py · …
    polling/coordinator.py · …
    messaging_pipeline/message_routing.py
  bot.py · bootstrap.py · cli.py · main.py        # lifecycle + entry

tests/
  ccslack/                                        # unit tests, mirror src
  conftest.py                                     # env stubs + fixtures

docs/
  setup.md · commands.md · configuration.md · architecture.md · development.md
```

Architecture deep-dive in [`docs/architecture.md`](architecture.md).

---

## Code conventions

- Every `.py` starts with a **module-level docstring** explaining what it
  does, in one sentence at the top + a few short responsibilities.
- **Full variable names**: `window_id` not `wid`, `channel_id` not `cid`,
  `session_id` not `sid`.
- **Catch specific exceptions** (`OSError`, `ValueError`,
  `SlackApiError`); never bare `except Exception` in production paths
  unless rebraced by background-task loops.
- **No comments inside test files** (and no docstrings on test functions
  either — the function name is the spec). Test files mirror the source
  tree under `tests/ccslack/`.
- **Lazy imports** for cycle-prone modules — annotate with
  `# Lazy: <reason>` so readers know why the import is inline.
- **Handlers depend on `SlackClient` Protocol**, not on
  `slack_sdk.AsyncWebClient`. Use `unwrap_web_client(client)` only when
  you need an SDK-only helper (rare).

---

## Adding a feature

A walkthrough using `/ccslack notes` as a hypothetical example.

1. **Add a subcommand branch** in `handlers/meta.py` — match `sub ==
   "notes"`, dispatch to `_handle_notes(client, channel_id, user_id, args)`.
2. **Implement the handler**. If it's substantial, put it in a new
   module (`handlers/notes.py`) and export a `handle_notes(...)`
   function. Use `BoltSlackClient(client)` if you need typed Slack
   calls.
3. **Auth**: call `is_authorized(user_id, channel_id)` for in-channel
   commands; `is_meta_authorized(user_id)` for meta-only.
4. **Add it to the slash command's `meta_only` set** (or not, depending
   on where it should be invokable).
5. **Update the help text** in `meta._help_text()`.
6. **Document** in `docs/commands.md` — every command must appear there.
7. **Add tests** in `tests/ccslack/test_<name>.py`. Use `FakeSlackClient`
   to assert the calls your handler makes, monkeypatched
   `thread_router` for routing dependencies.

If your feature persists state, also:
- Add a field to `WindowState` (`src/ccslack/window_state_store.py`) with
  default; update `to_dict` / `from_dict`.
- Document the new field in `docs/configuration.md` — *Per-channel settings*.

If your feature adds an action button:
- Pick a stable `action_id` prefix (`ccslack_notes_…`).
- Register the action handler via the relevant `register(app)` function.
- Document the button in `docs/commands.md` — *Block Kit actions*.

---

## Adding a new agent provider

1. Implement `providers/<name>.py` subclassing `AgentProvider`. Required:
   - `ProviderCapabilities` (`supports_hook`, `supports_hook_events`,
     `hook_event_types`, etc.).
   - `parse_transcript_line` and `parse_transcript_entries`.
   - Discovery helpers for `discover_commands` if needed.
2. Add the launch-command env var to `providers/__init__.py:
   resolve_launch_command`.
3. If the provider fires hooks, add a payload adapter in
   `hooks/adapters.py` that normalises its events into the canonical
   shape.
4. Update `hook.py:_install_hook` and the installer-related branches if
   ccslack should manage the install.
5. Add it to the `_SUPPORTED_PROVIDERS` tuple in `handlers/meta.py` and
   the toolbar layout map in `handlers/toolbar.py`.
6. Document in `docs/commands.md` (provider list) and
   `docs/configuration.md` (launch-command env var).

---

## Testing

```bash
make test         # 65 unit tests, < 1 s
```

Test patterns:

- **`FakeSlackClient`** — duck-typed implementation of `SlackClient`.
  Records every call in `.calls`; assert via `.call_count(method)`,
  `.last_call(method).kwargs`. Set `.returns[method] = {"ok": True,
  "ts": "1700.0001"}` to inject return values.
- **`conftest.py`** — auto-sets stub `SLACK_*` env vars at import time
  and provides a `ccslack_dir` fixture that monkeypatches
  `CCSLACK_DIR` to a per-test `tmp_path`.
- **Pure functions** — anything in `slack_formatting`, `slack_sender`'s
  split, `thread_router`, `shell_capture`'s strippers, `prompt_probe`'s
  classifier, etc. — test as plain Python functions. No mocks needed.
- **Async** — `asyncio_mode = "auto"` (in `pyproject.toml`), so `async
  def test_foo():` works without the `@pytest.mark.asyncio` decorator.

Add a test alongside any new behaviour. CI-style guard: `make check`
must pass before commit.

---

## Lint + format

`pyproject.toml` configures ruff with:

- `select = ["E4", "E7", "E9", "F", "ARG", "G", "BLE", "SIM", "PLR2004",
  "C901", "PLR0911", "PLR0912", "PLR0915", "N"]`
- max complexity 10 (some files have per-file overrides)
- max returns 8, max branches 15, max statements 60

Per-file overrides live in `[tool.ruff.lint.per-file-ignores]` — when
adding a complex new handler, prefer to break it into pure helpers
before adding it to the override list.

```bash
make fmt        # reformat
make lint       # check + show issues
uv run ruff check --fix src/  # autofix where possible
```

---

## Type checking

```bash
make typecheck
```

Pyright config in `pyproject.toml` (`[tool.pyright]`). Lightweight by
default — strict mode would fight with slack_sdk's typing.

---

## Commit conventions

The repo follows Conventional Commits (loosely):

- `feat: …`  — user-visible feature
- `fix: …`   — bug fix
- `chore: …` — scaffolding / tooling / docs
- `test: …`  — test-only changes
- `refactor: …` — internal reshuffle

Each commit body should explain *why* the change was needed and what
moved where. Sign with `Co-Authored-By:` when collaborating.

---

## Release (when we publish)

(Not yet wired — no CI, no PyPI yet. Future intent.)

Tag format: `vX.Y.Z`. CI would `uv build`, push to PyPI, generate a
GitHub release with the relevant changelog section.

---

## Debugging

Useful commands while developing:

```bash
# Local state snapshot
uv run ccslack status

# Verify hooks
uv run ccslack hook --status                  # Claude
uv run ccslack hook --status --provider codex # Codex

# Tail hook events
tail -f ~/.ccslack/events.jsonl

# Check session map
cat ~/.ccslack/session_map.json | python3 -m json.tool

# Run with verbose logging (already DEBUG by default in dev)
uv run ccslack 2>&1 | tee /tmp/ccslack.log
```

When a feature isn't firing, the usual culprits are:

1. **Hook not installed** for the provider you're testing.
2. **`session_map.json` missing the window** — SessionMonitor only
   watches sessions that appear there.
3. **`channel_bindings` empty** — bot was restarted before saving.
4. **Auth gate rejecting** — your user ID isn't in `ALLOWED_USERS` and
   the channel isn't bound yet.

In all four cases the relevant `logger.info` / `logger.debug` line will
say so. The bot logs are structlog → stderr (the hook subprocess too,
since the structlog-to-stderr config is applied in `hook_main`).
