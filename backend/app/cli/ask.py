"""Answer a single ask end-to-end from the CLI (design-spec §6; implementation-plan T4.6).

Run from the ``backend/`` directory:

    python -m app.cli.ask "what is the capital of France?"
    python -m app.cli.ask --db /tmp/x.db "hello"

Drives the request through the company roles (PM → Boss → Analyzer → Junior
Worker → PM) via the synchronous control loop, persisting the full trace, and
prints the validated answer. This path calls real models (network); the offline
end-to-end test drives the same control loop with a fake provider.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from collections.abc import Callable
from pathlib import Path

from dotenv import load_dotenv

from app.advisor.providers import AIProvider, MissingCredentialError, build_provider
from app.advisor.wrapper import Advisor
from app.config.settings import REPO_ROOT, ModelsConfig, load_models_config
from app.roles.control import AskOutcome, ensure_owner, run_ask
from app.runlog import setup_run_logging
from app.storage.db import connect
from app.storage.migrations import migrate

DEFAULT_DB_NAME = "app.db"

Getenv = Callable[[str], "str | None"]


def build_resolver(
    models: ModelsConfig, *, getenv: Getenv = os.getenv
) -> Callable[[str], AIProvider]:
    """Resolve a model-role → provider, building each provider once (cached)."""
    cache: dict[str, AIProvider] = {}

    def resolve(role: str) -> AIProvider:
        if role not in cache:
            cache[role] = build_provider(models.provider_for_role(role), getenv=getenv)
        return cache[role]

    return resolve


def _print_outcome(outcome: AskOutcome) -> None:
    if outcome.status in ("answered", "unanswered"):
        # Both carry a PM-formatted delivery (the answer, or an honest
        # "couldn't answer from a source" message).
        print(outcome.delivery)
    elif outcome.status == "needs_clarification":
        print(f"/req {outcome.request.code} needs clarification:")
        for question in outcome.clarify or []:
            print(f"  - {question}")
    elif outcome.status == "planned":
        print(
            f"/req {outcome.request.code} is a complex job (job #{outcome.job_id}); "
            "execution arrives in a later phase."
        )
    else:  # rejected
        print(f"/req {outcome.request.code} could not be routed; please rephrase.")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m app.cli.ask",
        description="Answer a single ask end-to-end through the company roles.",
    )
    parser.add_argument("prompt", help="the question to ask")
    parser.add_argument(
        "--db", type=Path, default=None, help="database file (default: data/app.db)"
    )
    parser.add_argument(
        "-d",
        "--debug",
        action="store_true",
        help="debug mode: stream all logs (incl. the model's response) to the console",
    )
    args = parser.parse_args(argv)

    load_dotenv(REPO_ROOT / ".env", override=False)

    # Every run is always logged (redacted) to backend/logs/ask-<ts>.log so the
    # full model response stays auditable on disk. In --debug mode the same logs
    # are *also* streamed to the console at DEBUG level, so you can see exactly
    # what happened (each advisor call, the model's reply, validation notes).
    log_path = setup_run_logging("ask", console_level=logging.DEBUG if args.debug else None)

    try:
        models = load_models_config()
        resolver = build_resolver(models)
    except (MissingCredentialError, KeyError, FileNotFoundError) as exc:
        print(f"[fail] configuration error: {exc}")
        return 1

    db_path = args.db or (REPO_ROOT / "data" / DEFAULT_DB_NAME)
    conn = connect(db_path)
    try:
        migrate(conn)
        advisor = Advisor(resolve_provider=resolver, conn=conn)
        user_id = ensure_owner(conn)
        outcome = run_ask(conn, advisor, args.prompt, user_id=user_id)
        _print_outcome(outcome)
    finally:
        conn.close()
    print(f"\n[log] full run log (model response included): {log_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
