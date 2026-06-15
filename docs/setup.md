# Setup Guide

Get the deterministic AI assistant **configured and ready to use** — for people
who want to *run* it, not work on the code. Once this guide is done, see
[usage.md](usage.md) for asking questions, running the bot, and pairing chat
accounts.

> Setting up a **development** environment instead (venv, pinned deps, tests)?
> That's in [development.md](development.md). Companion docs: [usage.md](usage.md)
> (use it after setup), [design-spec.md](design-spec.md),
> [implementation-plan.md](implementation-plan.md).

This guide assumes the code is **already on your machine** (a checkout) — setup
does **no git operations**. It installs the program's environment, writes config
(`.env`, `config/models.yaml`) + the local auth cache, and verifies everything.

---

## 1. Prerequisites

- **Python 3.12+** (3.10+ works), with the stdlib `venv` module. On
  Debian/Ubuntu: `sudo apt install python3-venv`.
- For the default **GitHub Copilot** AI route: a GitHub account with **Copilot
  access**. For the alternative route: a **GitHub Models** fine-grained PAT.
- Optional (chat bot): a **Telegram bot token** from [@BotFather](https://t.me/BotFather).

---

## 2. One command (recommended)

From the repo root:

```bash
./scripts/setup.sh
```

This runs two steps and leaves you with a runnable app:

1. **Installs the environment** — creates `.venv/` and installs the exact,
   hash-pinned dependencies. *(The mechanics — offline cache, install modes — are
   in [development.md](development.md) §2; you don't need them to get going.)*
2. **Runs the configuration wizard** (`python -m app.cli.setup`) — AI provider,
   Telegram token, owner — ending with a `verify --dry-run` check.

Pass-through: `./scripts/setup.sh --check` reports status without changing
anything; any extra args go to the wizard.

That's the whole setup. The sections below explain the configuration wizard and
verification in more detail if you want to run them on their own.

---

## 3. Configure the assistant

### The wizard (recommended)

Run it from `backend/`:

```bash
cd backend
../.venv/bin/python -m app.cli.setup
```

It walks three steps and **skips anything already configured** (so re-runs are
safe and never clobber working settings):

| Step | What it does |
|------|--------------|
| **AI provider** | Choose **Route A** (GitHub Copilot device-flow login) or **Route B** (GitHub Models PAT), and write the matching `config/models.yaml` route. |
| **Telegram** | Capture `TELEGRAM_BOT_TOKEN` into `.env` (optionally verified via `getMe`). Skip if you only use the CLI. |
| **Pairing** | Informational only — chat pairing is **request-and-approve at runtime** (no GitHub, no login): once the bot runs, a user messages it, the bot replies a one-time code, and you run `pair --approve <code>` to admit them. See [usage.md](usage.md) §3. |

It ends by running `verify --dry-run`. Useful flags:

```bash
../.venv/bin/python -m app.cli.setup --check                 # status only, no changes/network
../.venv/bin/python -m app.cli.setup --reconfigure           # re-ask every step
../.venv/bin/python -m app.cli.setup --reconfigure provider  # re-ask just one step
../.venv/bin/python -m app.cli.setup --skip-telegram-verify  # stay offline (no getMe)
```

### Manual configuration (alternative)

Two files hold all runtime config:

1. **`config/models.yaml`** — which provider/model each role uses (swap models
   here, never in code).
2. **`.env`** (git-ignored) — secrets + overrides. Start from the template:

   ```bash
   cp .env.example .env
   ```

**Route A — GitHub Copilot (default, no PAT).** Log in with your GitHub account:

```bash
cd backend
../.venv/bin/python -m app.cli.login        # approve the device code in your browser
```

Then point the model roles at Copilot in `config/models.yaml` (`kind:
github_copilot`, bare model ids like `gpt-4o`). The bearer comes from the
device-flow cache (`data/.auth/github.json`, perms 600) — no token in `.env`.

**Route B — GitHub Models (PAT).** Set `GITHUB_MODELS_TOKEN=<fine-grained PAT
with models: read>` in `.env` (optionally `GITHUB_ORG`), and keep `kind:
github_models` in `config/models.yaml`.

> Secrets live **only** in `.env` or the auth cache — never in the database,
> logs, or prompts. See [development.md](development.md) for how the `Secret`
> type enforces this in code.

---

## 4. Verify it works

```bash
cd backend

# Config-only, no network (default). Checks role->provider mapping + tokens.
../.venv/bin/python -m app.cli.verify --dry-run

# Live (opt-in): a real catalog lookup + a tiny completion to confirm the account.
../.venv/bin/python -m app.cli.verify --live
```

`--dry-run` exits 0 when the configuration is valid. If it reports a missing
token or unmapped role, re-run the wizard (`setup --reconfigure`) or fix `.env` /
`config/models.yaml`.

You're set up. Next: [usage.md](usage.md) — ask questions, run the Telegram bot,
and pair chat accounts.
