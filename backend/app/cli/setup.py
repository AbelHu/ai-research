"""First-run setup wizard (design-spec §13; implementation-plan T9.6).

Run from the ``backend/`` directory **inside an existing checkout** (it does no
git operations):

    python -m app.cli.setup                 # guided setup (skips what already works)
    python -m app.cli.setup --check         # report configured vs missing (no changes)
    python -m app.cli.setup --reconfigure   # re-ask every step
    python -m app.cli.setup --reconfigure provider   # re-ask one step

Takes an already-checked-out repo to a configured, verified, runnable app: pick
+ authenticate the AI provider, set the Telegram bot token, and establish + pair
the owner — **without hand-editing `.env` or `config/models.yaml`**. Each step
**detects existing config and skips by default**, so re-runs are safe; only
missing steps prompt. The wizard ends by running ``verify --dry-run``.

The orchestration (`run_setup`/`check`) is unit-tested offline with injected I/O;
``main`` wires real stdin/stdout + the live seams.
"""

from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Callable
from pathlib import Path

from dotenv import load_dotenv

from app.cli.verify import run_dry_run
from app.config.settings import DEFAULT_MODELS_CONFIG, REPO_ROOT, load_models_config
from app.setup.config_writer import ROUTE_COPILOT, ROUTE_MODELS, EnvFile, current_route
from app.setup.prompts import Prompter
from app.setup.steps import (
    KEPT,
    MISSING,
    StepResult,
    pairing_step,
    provider_step,
    telegram_step,
)
from app.storage.db import connect
from app.storage.migrations import migrate

DEFAULT_DB_NAME = "app.db"

Getenv = Callable[[str], "str | None"]


def _reconfigure(step: str, mode: str) -> bool:
    """Whether ``step`` should be re-asked given the ``--reconfigure`` mode."""
    return mode in ("all", step)


def _default_dry_run() -> int:
    """Re-load ``.env`` and run the config-only ``verify`` check (no network)."""
    load_dotenv(REPO_ROOT / ".env", override=True)
    models = load_models_config()
    return run_dry_run(models, os.getenv)


def check(
    conn,
    env: EnvFile,
    models_path: Path,
    *,
    auth=None,
    getenv: Getenv = os.getenv,
) -> list[StepResult]:
    """Report what's configured vs missing — **no prompts, no writes, no network**."""
    from app.storage.repos import identities as identities_repo

    if auth is None:
        from app.advisor.auth import GitHubCopilotAuth

        auth = GitHubCopilotAuth()

    route = current_route(models_path)
    if route == ROUTE_COPILOT:
        provider_ok = bool(auth.is_logged_in())
    elif route == ROUTE_MODELS:
        provider_ok = env.has_value("GITHUB_MODELS_TOKEN") or bool(getenv("GITHUB_MODELS_TOKEN"))
    else:
        provider_ok = False

    owner = identities_repo.expected_owner_login(conn)
    return [
        StepResult("AI provider", KEPT if provider_ok else MISSING, f"route={route}"),
        StepResult(
            "Telegram",
            KEPT if env.has_value("TELEGRAM_BOT_TOKEN") else MISSING,
            "bot token",
        ),
        StepResult("Owner pairing", KEPT if owner else MISSING, f"owner={owner or '—'}"),
    ]


def run_setup(
    conn,
    prompter: Prompter,
    env: EnvFile,
    models_path: Path,
    env_path: Path,
    *,
    reconfigure: str = "none",
    skip_telegram_verify: bool = False,
    dry_run_fn: Callable[[], int] = _default_dry_run,
    provider_kwargs: dict | None = None,
    telegram_kwargs: dict | None = None,
    pairing_kwargs: dict | None = None,
) -> int:
    """Run the steps in order, persist ``.env``, then verify. Returns an exit code.

    The ``*_kwargs`` seams forward injected callables to each step (tests use
    them; real runs leave them empty so the steps use their live defaults).
    """
    results: list[StepResult] = []

    results.append(
        provider_step(
            prompter,
            env,
            models_path,
            reconfigure=_reconfigure("provider", reconfigure),
            **(provider_kwargs or {}),
        )
    )
    results.append(
        telegram_step(
            prompter,
            env,
            skip_verify=skip_telegram_verify,
            reconfigure=_reconfigure("telegram", reconfigure),
            **(telegram_kwargs or {}),
        )
    )
    results.append(
        pairing_step(
            conn,
            prompter,
            reconfigure=_reconfigure("owner", reconfigure),
            **(pairing_kwargs or {}),
        )
    )

    # Persist any captured .env values (provider PAT / Telegram token).
    env.save(env_path)

    prompter.say("")
    prompter.say("Setup summary:")
    for result in results:
        marker = "ok " if result.ok else "!! "
        label = "configured (kept)" if result.status == KEPT else result.status
        prompter.say(f"  [{marker}] {result.name}: {label} — {result.detail}")

    prompter.say("")
    prompter.say("Verifying configuration (dry-run, no network)…")
    return dry_run_fn()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m app.cli.setup",
        description="Configure + verify this checkout (AI provider, Telegram, owner pairing).",
    )
    parser.add_argument(
        "--db", type=Path, default=None, help="database file (default: data/app.db)"
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="report what's configured vs missing, then exit (no changes, no network)",
    )
    parser.add_argument(
        "--reconfigure",
        nargs="?",
        const="all",
        default="none",
        choices=["all", "provider", "telegram", "owner", "none"],
        help="re-ask every step, or a single step (provider|telegram|owner)",
    )
    parser.add_argument(
        "--skip-telegram-verify",
        action="store_true",
        help="don't call getMe to verify the Telegram token (stay offline)",
    )
    args = parser.parse_args(argv)

    load_dotenv(REPO_ROOT / ".env", override=False)
    env_path = REPO_ROOT / ".env"
    env = EnvFile.load(env_path)
    models_path = DEFAULT_MODELS_CONFIG
    db_path = args.db or (REPO_ROOT / "data" / DEFAULT_DB_NAME)
    conn = connect(db_path)
    try:
        migrate(conn)
        if args.check:
            results = check(conn, env, models_path)
            print("Configuration status:")
            all_ok = True
            for result in results:
                state = "configured" if result.ok else "MISSING"
                all_ok = all_ok and result.ok
                print(f"  - {result.name}: {state} ({result.detail})")
            return 0 if all_ok else 1

        prompter = Prompter()
        return run_setup(
            conn,
            prompter,
            env,
            models_path,
            env_path,
            reconfigure=args.reconfigure,
            skip_telegram_verify=args.skip_telegram_verify,
        )
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
