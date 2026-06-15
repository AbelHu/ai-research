# Development Guide

How to develop, test, and contribute to the deterministic AI assistant backend.

> **Just want to install and use it?** See [setup.md](setup.md) (install +
> configure) and [usage.md](usage.md) (ask questions, run the bot, pair
> accounts). This guide is for **working on the code**.
>
> Companion docs: [setup.md](setup.md), [usage.md](usage.md),
> [design-spec.md](design-spec.md) (what we're building and why),
> [implementation-plan.md](implementation-plan.md) (the task-by-task build plan),
> [db-schema.md](db-schema.md) (the storage-layer schema reference).

---

## 1. Prerequisites

- **Python 3.12+** (the project targets 3.10+, CI/dev uses 3.12) with the stdlib
  `venv` module. This machine has no system `pip` and only `python3` (no bare
  `python`), so always use the repo-root venv interpreter, `../.venv/bin/python`
  (run from `backend/`).
- On Debian/Ubuntu the `venv` module is `sudo apt install python3-venv`.

---

## 2. Setting up the development environment

### Install (recommended: the script)

From the repo root:

```bash
./scripts/setup-env.sh            # auto: offline if vendor/wheels exists, else PyPI
./scripts/setup-env.sh --offline  # require the wheelhouse; fail if it's missing
./scripts/setup-env.sh --online   # ignore the wheelhouse; fetch from PyPI
```

It creates `.venv/` and installs the **exact, pinned** dependencies from
[`backend/requirements.lock`](../backend/requirements.lock) with
`--require-hashes`, so any package whose bytes don't match the lock is rejected
(a supply-chain guard — §2.1). If the offline wheel cache
[`vendor/wheels/`](../vendor/README.md) is present the install runs **fully
offline**; otherwise it fetches the pinned versions from PyPI (still
hash-verified). After it runs, the package is importable as `app.*` (editable
install) and the CLIs work.

> End users get the environment **and** the configuration wizard in one step via
> `./scripts/setup.sh` — see [setup.md](setup.md). This section is the
> environment + dependency detail behind it.

### 2.1 Why pinned versions + hashes?

We build against **fixed versions** recorded in `backend/requirements.lock`
(exact `==` pins plus `sha256` hashes). Versions change only when we
*deliberately* bump them — never implicitly. This protects against pulling a
newly published, possibly back-doored release of a dependency: with
`--require-hashes`, pip installs only the precise artifacts we've vetted and
refuses anything else.

### 2.2 Offline cache (wheelhouse)

[`vendor/wheels/`](../vendor/README.md) holds every dependency wheel (and the
`setuptools`/`wheel` build backend) so a machine with no internet can build the
whole environment. It is **git-ignored** — the wheels are large,
platform-specific binaries and are not pushed to the remote. The committed
source of truth is `backend/requirements.lock`; the cache is rebuilt from it.

After a fresh clone the wheelhouse is empty. You only need to repopulate it if
you intend to install **offline** afterwards (otherwise `setup-env.sh` fetches
the pinned versions from PyPI directly):

```bash
./scripts/fetch-wheels.sh         # download the exact, hash-pinned versions
./scripts/setup-env.sh --offline  # then install with no network
```

`fetch-wheels.sh` does **not** change the lock — it only downloads what the lock
already pins, verifying every `sha256`. The cache is platform-specific (built
for Linux x86_64 / CPython 3.12 here).

### 2.3 Bumping dependency versions (maintainers)

Change versions only on purpose, then refresh the lock + cache **online**:

```bash
# 1. widen/raise a version range in backend/pyproject.toml, then resolve it:
./.venv/bin/python -m pip install -e backend[dev]
# 2. freeze the new resolved set, rebuild the wheelhouse, regenerate the lock:
./scripts/lock-deps.sh
# 3. run the tests, review the requirements.lock diff, and commit.
```

### Manual setup (without the script)

```bash
# Create the venv if it does not already exist.
python3 -m venv .venv

# Install the backend in editable mode with dev tooling (pytest + ruff).
cd backend
../.venv/bin/python -m pip install -e ".[dev]"
```

> Note: the manual path resolves the latest versions allowed by `pyproject.toml`
> (not the pinned lock) and needs the network. Prefer the setup script for a
> reproducible environment.

After setup, the package is importable as `app.*` and the `ai-research-backend`
distribution is installed in editable mode (source edits take effect immediately).

### Activating the venv (optional, for convenience)

```bash
source .venv/bin/activate   # from the repo root; now `python` == the venv
cd backend
python -m pytest -q
```

The rest of this guide uses the explicit `../.venv/bin/python` form (run from
`backend/`) so the commands work whether or not the venv is activated.

---

## 3. Project layout

```
backend/
  pyproject.toml            # abstract deps (version ranges), pytest + ruff config
  requirements.lock         # exact, hash-pinned deps for reproducible installs
  app/
    security.py             # Secret type + REDACTED placeholder (never log secrets)
    advisor/
      providers.py          # AI provider transport (OpenAI-compatible / GitHub Models)
      redaction.py          # outbound secret-scrubbing guard
    cli/                    # entry points (see usage.md): setup, ask, login, pair, telegram, verify, db
      setup.py              # `python -m app.cli.setup` first-run configuration wizard
      verify.py             # `python -m app.cli.verify` config/auth check
      db.py                 # `python -m app.cli.db` migrate + inspect the schema
    config/
      settings.py           # env-backed Settings + models.yaml loader
      policies.py           # typed policy knobs loader
    storage/
      db.py                 # SQLite connect() (WAL, foreign keys, Row factory)
      migrations/           # forward-only SQL migrations + runner (see db-schema.md)
      repos/                # typed repositories (requests/jobs, memories)
  tests/                    # pytest suite (runs fully offline)
config/
  models.yaml               # role -> provider/model mapping (swap models here, not in code)
  policies.yaml             # tunable limits (declines, concurrency, TTLs, ...)
scripts/
  setup.sh                  # one command: env + the configuration wizard (see setup.md)
  setup-env.sh              # create .venv + install pinned deps (offline-first)
  fetch-wheels.sh           # repopulate vendor/wheels from the lock (after clone)
  lock-deps.sh              # (maintainer) refresh the wheelhouse + requirements.lock
  _gen_lock.py              # helper: hash-pin a wheelhouse into requirements.lock
vendor/
  wheels/                   # offline dependency cache (wheelhouse, git-ignored); see vendor/README.md
docs/                       # design spec, implementation plan, db schema, this guide
.env.example                # copy to .env and fill in secrets (git-ignored)
```

---

## 4. Handling secrets in code

Runtime configuration (`config/models.yaml` + `.env`) is covered in
[setup.md](setup.md) §3. This section is about how the **code** keeps secrets safe.

Secrets (API tokens, keys) are wrapped in the [`Secret`](../backend/app/security.py)
type so they can never leak through logs, reprs, tracebacks, or string
interpolation. The real value is reachable only via `reveal()`, called at the
exact boundary where it is needed (e.g. building an `Authorization` header).

```python
from app.security import Secret

token = Secret("ghp_realtokenvalue")
print(token)            # [REDACTED]
print(f"auth={token}")  # auth=[REDACTED]
logging.info("%s", token)   # logs [REDACTED]
token.reveal()          # "ghp_realtokenvalue"  <- only at the point of use
```

`Settings` token fields are typed `Secret | None`, so a dumped or logged
settings object is automatically redacted. Rules of thumb:

- Never `print`/log a raw token; pass the `Secret` and let it redact.
- Call `.reveal()` only at the transport boundary, never earlier.
- Outbound model payloads also pass through `app.advisor.redaction` as a second
  line of defense.

---

## 5. Running & using the assistant

The runnable CLIs — asking a question (`app.cli.ask`), running the Telegram bot
(`app.cli.telegram`), pairing chat accounts (`app.cli.pair`), login, and DB
inspection — are documented in [usage.md](usage.md). The config-only check used
throughout development is:

```bash
cd backend
../.venv/bin/python -m app.cli.verify --dry-run   # validate config, no network
```

---

## 6. Running the tests

There are **two suites**: the default **unit** suite (fully offline) and an
opt-in **integration** suite that calls a **real** AI model. Every run also
writes a **detailed, secret-free log file** you can audit (see §6.3).

### 6.1 Unit tests (default — offline, deterministic)

The default suite runs **offline**: an autouse guard in
[tests/conftest.py](../backend/tests/conftest.py) raises if any test opens a real
network socket, and the live `integration` tests are **deselected by default**.

```bash
cd backend

# Whole unit suite (integration tests are excluded automatically)
../.venv/bin/python -m pytest -q

# A single file / a single test
../.venv/bin/python -m pytest tests/test_security.py -q
../.venv/bin/python -m pytest tests/test_security.py::test_reveal_returns_value -q

# Verbose (per-test PASS/FAIL) and show prints
../.venv/bin/python -m pytest -v
../.venv/bin/python -m pytest -v -s
```

If a unit test needs a model, it uses a **fake provider** or an
`httpx.MockTransport` — never the network.

### 6.2 Integration tests (opt-in — real AI model)

These call a **real** model end-to-end. They are marked `integration`,
**excluded from the default run**, and **skipped** (not failed) unless a model
provider is configured — so running them without credentials is safe.

**First, configure a provider** (one of):

- **Route A — device-flow login (no PAT):**
  ```bash
  ../.venv/bin/python -m app.cli.login        # approve the device code in your browser
  ```
  then point the model roles at GitHub Copilot in `config/models.yaml`
  (`kind: github_copilot`, bare model ids like `gpt-4o`).
- **Route B — GitHub Models PAT:** set `GITHUB_MODELS_TOKEN` in `.env` and keep
  `kind: github_models` in `config/models.yaml`.

> Tip: `../.venv/bin/python -m app.cli.verify --dry-run` reports whether your
> chosen provider is ready (a token is set, or you're logged in).

**Then run them:**

```bash
cd backend

# Run only the live integration tests (real model calls)
../.venv/bin/python -m pytest -m integration -v

# Show skip reasons (e.g. "not logged in" / token unset)
../.venv/bin/python -m pytest -m integration -rs

# Everything: unit + integration
../.venv/bin/python -m pytest -m "integration or not integration"
```

What they prove: the real model's output survives our strict template-schema
validation (anti-hallucination), and a simple ask runs end-to-end. **Skipped**
means unconfigured; real timing (e.g. `5 passed in 12s`) means live calls
actually happened.

### 6.3 Auditing the test logs

Every `pytest` run writes one timestamped log file under **`backend/logs/`**
(git-ignored), labelled by suite:

```
backend/logs/unit-YYYYMMDD-HHMMSS.log          # a default (unit) run
backend/logs/integration-YYYYMMDD-HHMMSS.log   # a `-m integration` run
```

The path is printed at the end of every run (`Detailed log written to: …`).
Each **test case** is a delimited section containing:

- `----- START <test id> -----`
- the application logs emitted during it — notably **each advisor model call**
  (`advisor call: role=… template=… model=…`) and its **result**
  (`advisor result: … status=valid|repaired|fallback|failed tokens=… latency_ms=…`);
  a non-`valid` reply also logs the model's (redacted) response so you can see
  *why* validation failed;
- the outcome line — `PASSED` / `FAILED` / `SKIPPED (reason)` with duration;
- on failure, the full `TRACEBACK …`.

**Secrets never appear:** a redaction filter scrubs any token-looking text from
every record before it is written (defense-in-depth on top of the `Secret` type),
so the files are safe to keep and share.

```bash
# Tail the most recent log
ls -t backend/logs/*.log | head -1 | xargs tail -f

# Just the model calls from the last integration run
grep "advisor " "$(ls -t backend/logs/integration-*.log | head -1)"

# Every failure with its traceback
grep -A20 "TRACEBACK" "$(ls -t backend/logs/*.log | head -1)"
```

> Note: integration tests use an in-memory DB, so their `ai_calls` rows are not
> persisted. For a **durable** model-call audit, run the `ask` CLI (it writes to
> `data/app.db`) and query the `ai_calls` table:
> ```bash
> ../.venv/bin/python -m app.cli.ask "what is 2+2?"
> ../.venv/bin/python -m app.cli.db --schema   # confirms the DB + tables
> ```
> `ai_calls` stores prompt/response as **SHA-256 refs** (no raw text), by design
> (§12); the readable model output is in the run log above and the CLI output.

---

## 7. Linting & formatting

We use [ruff](https://docs.astral.sh/ruff/) for both linting and formatting
(config in [pyproject.toml](../backend/pyproject.toml)).

```bash
cd backend

../.venv/bin/python -m ruff check .            # lint
../.venv/bin/python -m ruff check . --fix      # lint + autofix
../.venv/bin/python -m ruff format .           # apply formatting
../.venv/bin/python -m ruff format --check .   # verify formatting (CI-style)
```

Import order ruff expects: `from __future__ import annotations`, then stdlib,
then third-party, then first-party `app.*` — each group separated by a blank
line.

---

## 8. The per-task development workflow

This project is built **one small, validated step at a time** (see the
implementation plan). For every change:

1. **One thing per step** — change one logical thing; no drive-by edits.
2. **Add or update a test** for the new behavior.
3. **Validate before moving on** — all of the following must pass:

   ```bash
   cd backend
   ../.venv/bin/python -m pytest -q                 # tests green
   ../.venv/bin/python -m ruff check .              # lint clean
   ../.venv/bin/python -m ruff format --check .     # format clean
   ../.venv/bin/python -m app.cli.verify --dry-run  # config check (no network)
   ```

4. No secrets, no unrelated diffs; existing tests still pass.

---

## 9. Troubleshooting

- **`python: command not found`** — this machine only has `python3`, and the
  project deps live in the venv. Use `../.venv/bin/python` (from `backend/`) or
  activate the venv first.
- **`ModuleNotFoundError: No module named 'app'` / `pydantic`** — dependencies
  aren't installed in the interpreter you're using. Re-run the install step
  (section 2) with the venv interpreter.
- **`ensurepip is not available` when creating the venv** — the base Python is
  missing the `venv`/`ensurepip` module. On Debian/Ubuntu:
  `sudo apt install python3-venv` (or `python3.12-venv`), then re-run
  `scripts/setup-env.sh`.
- **`THESE PACKAGES DO NOT MATCH THE HASHES` during install** — a wheel's bytes
  don't match `requirements.lock`. Either the cache is stale/corrupt, or someone
  changed a package. Don't bypass it: rebuild the cache from a trusted machine
  with `scripts/lock-deps.sh` and re-review the lock diff.
- **A just-edited file looks stale in the terminal** (e.g. `grep`/import can't
  find a symbol you just added) — on a Windows drive-mounted (drvfs) workspace,
  terminal reads can briefly lag editor writes. The write did land; re-run the
  command a moment later. `cat -n <file>` will then show the current content.
- **A test hit the network guard (`NetworkAccessError`)** — the test tried real
  I/O. Replace it with a fake provider or an `httpx.MockTransport`.
