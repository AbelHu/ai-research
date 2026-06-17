# Usage Guide

How to **use** the deterministic AI assistant once it's set up. If you haven't
configured it yet, start with [setup.md](setup.md).

> Companion docs: [setup.md](setup.md) (install + configure), [development.md](development.md)
> (develop/test), [design-spec.md](design-spec.md), [implementation-plan.md](implementation-plan.md).

All commands run from the `backend/` directory using the repo-root virtualenv
interpreter:

```bash
cd backend
```

---

## 1. Ask a question from the CLI

The quickest way to use the assistant — no chat app required:

```bash
../.venv/bin/python -m app.cli.ask "what is the capital of France?"
```

It drives the request through the full company pipeline (PM → Boss → Analyzer →
Junior Worker) and prints a validated answer with its sources. Flags:

```bash
../.venv/bin/python -m app.cli.ask -d "explain go memory management"   # debug: stream logs to console
../.venv/bin/python -m app.cli.ask --db /tmp/x.db "hello"              # use a specific database file
```

Every run also writes a redacted log to `backend/logs/ask-<timestamp>.log`
(the path is printed at the end) — it includes the exact prompt sent to the
model and the model's full response, for auditing.

> First time? This calls a **real** model, so the AI provider must be configured
> (see [setup.md](setup.md) §4). `app.cli.verify --dry-run` confirms readiness.

---

## 2. Run the Telegram bot

After setting `TELEGRAM_BOT_TOKEN` (via the wizard or `.env`), start the bot:

```bash
../.venv/bin/python -m app.cli.telegram          # long-poll; Ctrl-C to stop
../.venv/bin/python -m app.cli.telegram -d       # debug: stream logs (incl. model responses)
../.venv/bin/python -m app.cli.telegram --once   # drain pending updates once, then exit (smoke)
```

> **Running as a service?** `python -m app.cli.web` starts this same gateway in
> the background **and** the dashboard in one process — see §5. Use the
> standalone runner above when you want the bot **only**.

The bot is **closed to everyone except paired accounts** — a publicly reachable
bot must refuse strangers. So before you (or anyone) can chat with it, the chat
account has to be **paired** to the owner. That's §3.

---

## 3. Pair a chat account

Pairing binds a chat identity `(channel, user)` to the **owner** so it may talk
to the bot. Everyone else is refused (and the refusal is rate-limited + audited).
Pairing is **deterministic** — the AI is never consulted on who may chat. There
are three ways; the first is the everyday one.

### 3a. Request-and-approve (recommended)

User-initiated, **approved by the operator on the trusted console**:

1. The user sends **any message** to the bot from their chat app.
2. The bot replies with a short **pairing code**, e.g.:

   > You're not paired with this assistant yet.
   > Your pairing code is: `X496-8GMU`
   > Ask the operator to approve it by running: `pair --approve X496-8GMU`

3. On the host, the operator lists pending requests and approves one:

   ```bash
   ../.venv/bin/python -m app.cli.pair --list
   ../.venv/bin/python -m app.cli.pair --approve X496-8GMU
   ```

4. The user is admitted from their next message onward.

A user who messages repeatedly keeps the **same** code (no spam), and the bot
never creates a request or job for an unpaired sender.

### 3b. Host one-time code

Operator-initiated: mint a code on the host, then the user types it in chat.

```bash
../.venv/bin/python -m app.cli.pair          # mint a single-use code (default action)
# → user sends:  /pair <code>   from their chat account
```

### 3c. Device-flow owner challenge

Bind a specific account by approving the GitHub device flow **as the owner**
(the bot verifies your GitHub login matches the owner):

```bash
../.venv/bin/python -m app.cli.pair --challenge --channel telegram --user 123456789
```

### Manage pairings

```bash
../.venv/bin/python -m app.cli.pair --list                 # pending requests + codes + paired accounts
../.venv/bin/python -m app.cli.pair --revoke telegram:123456789   # revoke an account (can no longer chat)
```

> Finding a Telegram user id: message the bot with the runner in `-d` mode and
> read the `inbound from telegram:<id>` log line, or use a userinfo bot.

---

## 4. Manage the GitHub login

The AI provider login (Route A) lives in a git-ignored, permission-restricted
cache (`data/.auth/github.json`). Manage it with:

```bash
../.venv/bin/python -m app.cli.login            # start/refresh the device-flow login
../.venv/bin/python -m app.cli.login --status   # is a token cached?
../.venv/bin/python -m app.cli.login --logout    # remove the cached credentials
../.venv/bin/python -m app.cli.login --no-browser  # don't auto-open the browser
```

---

## 5. Run as a service (gateway + dashboard)

`python -m app.cli.web` is the long-running **service** entry point. In one
process it:

* runs the **Telegram gateway** in the background (long-poll → answer paired
  users), exactly like `app.cli.telegram`,
* runs the **job worker** that executes **planned jobs** (a complex task/feature,
  or a simple ask that needed planning) end-to-end — Senior Worker runs the
  plan's tasks, the Plan Expert resolves phases, the Company Expert signs off —
  then delivers the result back to the originating chat, quoting your original
  message and tagged with its `/req` id, and
* serves a read-only **dashboard + JSON API** — requests, the
  job→plan→phase→task tree, host/model usage, stored memories, and paired
  accounts.

It runs on the **stdlib** (no extra dependency) and **blocks until Ctrl+C**:

```bash
../.venv/bin/python -m app.cli.web                 # gateway + job worker + dashboard
../.venv/bin/python -m app.cli.web --port 9000     # choose a dashboard port
../.venv/bin/python -m app.cli.web --no-bot        # dashboard only (no gateway/worker)
../.venv/bin/python -m app.cli.web -d              # debug: stream logs to the console
../.venv/bin/python -m app.cli.web --db /tmp/x.db  # use a specific database file
```

The gateway + job worker start automatically when `TELEGRAM_BOT_TOKEN` is set;
without a token (or with `--no-bot`) the dashboard runs standalone. Paired-account
rules (§3) still apply — the gateway answers only paired users. You can also run
the worker on its own with `python -m app.cli.jobworker` (`--once` drains the
queue and exits).

> **Quick vs. planned replies.** A simple ask is answered immediately. Anything
> that needs planning is acknowledged right away (`/req … I'll work through it`)
> and the **final answer arrives as a follow-up** once the job worker finishes —
> quoting your original message so it's clear which request it answers.

Open `http://127.0.0.1:8000` in a local browser for the HTML view, or use the
JSON API directly (handy for scripting/testing). The HTML dashboard
**auto-refreshes every 10 seconds** so the data stays live without reloading by
hand:

| Route | Returns |
|-------|---------|
| `GET /healthz` | `{"status":"ok"}` liveness probe |
| `GET /api/requests` | request index (newest first) |
| `GET /api/requests/<id>` | one request's job→plan→phase→task tree + steps + `ai_calls` |
| `GET /api/system` | host metrics (CPU/mem/disk) + model usage from `ai_calls` |
| `GET /api/memories` | the active memories (newest first) — kind, summary, importance, use count |
| `GET /api/accounts` | the paired-account allowlist |
| `POST /api/accounts/<channel>/<user>/revoke` | revoke an account |

```bash
# Quick check from another terminal while it runs:
curl -s http://127.0.0.1:8000/healthz
curl -s http://127.0.0.1:8000/api/system
```

> **No authentication yet** — it binds to `127.0.0.1` (local only) by design.
> `--host 0.0.0.0` exposes it on the network; only do that behind a trusted
> boundary. A future cloud deployment puts auth + TLS in front of this same app.

---

## 6. Inspect the database

State lives in `data/app.db` (SQLite). Create/inspect the schema:

```bash
../.venv/bin/python -m app.cli.db --migrate   # apply migrations (idempotent)
../.venv/bin/python -m app.cli.db --schema    # print the tables
```

A durable audit of model calls is in the `ai_calls` table (prompt/response
stored as SHA-256 refs, never raw text — §12). The readable prompt/response is
in the run logs (§6).

---

## 7. Where things are written

| Location | Contents |
|----------|----------|
| `data/app.db` | SQLite state: requests, jobs, steps, role messages, `ai_calls`, identities, pairings, memory. |
| `data/.auth/github.json` | The device-flow auth cache (perms 600, git-ignored). Never in the DB or logs. |
| `data/library/…` | The on-disk library: per-request folders + final reports. |
| `backend/logs/ask-*.log`, `backend/logs/telegram-*.log` | Redacted run logs — each advisor call, the model's full response, validation notes. Safe to keep/share (secrets are scrubbed). |

---

## 8. Common issues

- **"You're not paired" when chatting** — the account isn't on the allowlist;
  pair it (§3). Only the owner's accounts are admitted by design.
- **`ask` / `telegram` fails to start with a configuration error** — the AI
  provider isn't ready. Run `app.cli.verify --dry-run`, then `app.cli.setup` or
  `app.cli.login` (see [setup.md](setup.md)).
- **`TELEGRAM_BOT_TOKEN is not set`** — set it via `app.cli.setup` or in `.env`.
- **The bot replies to no one** — confirm it's running (§2) and that your
  account is paired (§3); unpaired senders only ever get a pairing code.
