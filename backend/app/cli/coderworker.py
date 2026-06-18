"""Coder worker — the dedicated, privileged codegen lane (P4).

A separate long-running process (distinct from the gateway and the job worker)
that drains the **coder queue**: it claims a feature job's coding request, runs
the agentic Coder loop (generate → sandbox-validate → repair → promote inert) via
:func:`app.coder.agent.run_coder`, records the result on the queue row, and
delivers a follow-up to the originating chat ("built + verified; confirm to
activate").

This lane needs more local permissions than the job worker — it writes files and
spawns the validation sandbox — so it runs as its **own process** for privilege
separation. One bad request never stops the loop.

Run from the ``backend/`` directory:

    python -m app.cli.coderworker          # drain forever; Ctrl-C to stop
    python -m app.cli.coderworker --once   # drain queued coding requests, then exit
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from collections.abc import Callable
from pathlib import Path

from dotenv import load_dotenv

from app.advisor.providers import MissingCredentialError
from app.advisor.wrapper import Advisor
from app.channels.adapter import OutboundMessage
from app.coder.agent import CoderOutcome, run_coder
from app.config.settings import REPO_ROOT, get_settings, load_models_config
from app.runlog import setup_run_logging
from app.storage.db import connect
from app.storage.migrations import migrate
from app.storage.repos import coder_queue as coder_queue_repo

logger = logging.getLogger("app.cli.coderworker")

DEFAULT_DB_NAME = "app.db"
DEFAULT_IDLE_SECONDS = 2.0

SendFn = Callable[[OutboundMessage], None]


def _deliver(send: SendFn | None, cjob: coder_queue_repo.CoderJob, text: str) -> None:
    """Send a coding request's result to its originating chat, if addressable."""
    if send is None or not cjob.chat_id or not text:
        return
    send(
        OutboundMessage(
            channel=cjob.channel or "",
            chat_id=cjob.chat_id,
            text=text,
            reply_to_message_id=cjob.reply_to_message_id,
        )
    )


def _success_message(job_code: str, skill_modules: list[str]) -> str:
    mods = ", ".join(f"`{m}`" for m in skill_modules) or "a new skill"
    return (
        f"I built and verified a reusable skill for this ({mods}). It's saved but "
        f"inactive pending your review — run `python -m app.cli.confirm {job_code}` "
        "to activate it."
    )


def _validation_summary(outcome: CoderOutcome) -> dict:
    return {
        "summary": outcome.report.summary if outcome.report else None,
        "iterations": outcome.iterations,
    }


def _process_one(
    conn, advisor: Advisor, send: SendFn | None, cjob: coder_queue_repo.CoderJob
) -> None:
    """Run one coding request to completion; record + deliver, or record failure."""
    try:
        outcome = run_coder(
            advisor,
            job_code=cjob.job_code,
            goal=cjob.goal,
            request_id=cjob.request_id,
            job_id=cjob.job_id,
        )
    except Exception as exc:  # noqa: BLE001 - one bad request must not kill the worker
        logger.error("coder job %s crashed: %s", cjob.job_id, exc)
        coder_queue_repo.mark_failed(conn, cjob.job_id, str(exc))
        return

    validation = _validation_summary(outcome)
    if outcome.ok:
        coder_queue_repo.mark_done(
            conn, cjob.job_id, skill_modules=outcome.skill_modules, validation=validation
        )
        logger.info("coder job %s done: %s", cjob.job_id, outcome.skill_modules)
        _deliver(send, cjob, _success_message(cjob.job_code, outcome.skill_modules))
    else:
        coder_queue_repo.mark_failed(
            conn, cjob.job_id, outcome.error or "validation failed", validation=validation
        )
        logger.warning("coder job %s failed: %s", cjob.job_id, outcome.error)
        _deliver(
            send,
            cjob,
            "I tried to build a reusable skill for this but couldn't get it to pass "
            "validation, so I'm leaving it out for now.",
        )


def serve_coder_jobs(
    conn,
    advisor: Advisor,
    send: SendFn | None = None,
    *,
    once: bool = False,
    idle_seconds: float = DEFAULT_IDLE_SECONDS,
    on_idle_sleep: Callable[[float], None] = time.sleep,
) -> int:
    """Drain the coder queue, generating + verifying + delivering each request.

    ``once`` drains every currently-queued coding request and returns; otherwise
    it loops forever, idling between empty polls. ``send``/``on_idle_sleep`` are
    injectable seams for tests.
    """
    logger.info("coder worker started (once=%s)", once)
    while True:
        cjob = coder_queue_repo.claim_next(conn)
        if cjob is None:
            if once:
                return 0  # queue drained
            on_idle_sleep(idle_seconds)
            continue
        _process_one(conn, advisor, send, cjob)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m app.cli.coderworker",
        description="Run feature codegen requests from the coder queue (privileged lane).",
    )
    parser.add_argument(
        "--db", type=Path, default=None, help="database file (default: data/app.db)"
    )
    parser.add_argument("--once", action="store_true", help="drain queued requests, then exit")
    parser.add_argument(
        "-d", "--debug", action="store_true", help="stream logs (incl. model responses) to console"
    )
    args = parser.parse_args(argv)

    load_dotenv(REPO_ROOT / ".env", override=False)
    setup_run_logging("coderworker", console_level=logging.DEBUG if args.debug else logging.INFO)

    try:
        models = load_models_config()
    except (MissingCredentialError, KeyError, FileNotFoundError) as exc:
        print(f"[fail] configuration error: {exc}")
        return 1

    db_path = args.db or (REPO_ROOT / "data" / DEFAULT_DB_NAME)
    conn = connect(db_path)
    try:
        migrate(conn)
        send: SendFn | None = None
        settings = get_settings()
        token = settings.telegram_bot_token
        if token is not None and token.reveal().strip():
            from app.channels.telegram import TelegramAdapter

            send = TelegramAdapter(token, webhook_secret=settings.telegram_webhook_secret).send

        from app.cli.ask import build_resolver

        advisor = Advisor(resolve_provider=build_resolver(models), conn=conn)
        print("[ok]   Coder worker running. Press Ctrl-C to stop.")
        return serve_coder_jobs(conn, advisor, send, once=args.once)
    except KeyboardInterrupt:
        print("\n[ok]   Stopped.")
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
