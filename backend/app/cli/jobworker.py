"""Background job worker — runs planned jobs and delivers results (slice B2).

The second long-running loop of the service (alongside the Telegram gateway). It
drains the **job queue**: claims the oldest planned job, runs it end-to-end via
:func:`app.roles.execution.execute_planned_job` (Senior Worker + Plan Expert +
sign-off), records the result, and delivers the follow-up reply back to the
originating chat — quoting the user's original message so it's tied to its
``/req``.

Run from the ``backend/`` directory:

    python -m app.cli.jobworker          # drain forever; Ctrl-C to stop
    python -m app.cli.jobworker --once   # drain currently-queued jobs, then exit

Like the gateway, a transient failure on one job never stops the loop — the job
is marked ``failed`` (kept for inspection) and the worker moves on.
"""

from __future__ import annotations

import argparse
import logging
import re
import sys
import time
from collections.abc import Callable
from pathlib import Path

from dotenv import load_dotenv

from app.advisor.providers import MissingCredentialError
from app.advisor.wrapper import Advisor
from app.channels.adapter import OutboundMessage
from app.config.policies import get_policies
from app.config.settings import REPO_ROOT, get_settings, load_models_config
from app.roles.execution import execute_planned_job
from app.runlog import setup_run_logging
from app.storage.db import connect
from app.storage.migrations import migrate
from app.storage.repos import job_queue as job_queue_repo

logger = logging.getLogger("app.cli.jobworker")

DEFAULT_DB_NAME = "app.db"
# How long to idle between polls when the queue is empty (service mode).
DEFAULT_IDLE_SECONDS = 2.0

# A delivery sink: send an outbound reply over a channel (e.g. ``adapter.send``).
SendFn = Callable[[OutboundMessage], None]

_RETRYABLE_ERROR_PATTERNS = (
    re.compile(r"timeout|timed out", re.IGNORECASE),
    re.compile(r"connection|network|temporar", re.IGNORECASE),
    re.compile(r"429|rate\s*limit|too many requests", re.IGNORECASE),
    re.compile(r"5\d\d|server error|bad gateway|service unavailable", re.IGNORECASE),
)


def _is_retryable_error(exc: Exception) -> bool:
    text = str(exc)
    return any(p.search(text) for p in _RETRYABLE_ERROR_PATTERNS)


def _deliver(send: SendFn | None, job: job_queue_repo.QueuedJob, text: str) -> None:
    """Send a finished job's result to its originating chat, if addressable.

    No-op when there's no sink (CLI-only run) or the job has no chat address
    (e.g. a CLI-originated job): the result is still recorded on the queue row.
    """
    if send is None or not job.chat_id or not text:
        return
    send(
        OutboundMessage(
            channel=job.channel or "",
            chat_id=job.chat_id,
            text=text,
            reply_to_message_id=job.reply_to_message_id,
        )
    )


def _process_one(
    conn, advisor: Advisor, send: SendFn | None, job: job_queue_repo.QueuedJob
) -> None:
    """Run one claimed job to completion; record + deliver, or record failure.

    A failure is caught and stored (``failed``) so a single bad job never takes
    down the worker — the same resilience the gateway has.
    """
    try:
        outcome = execute_planned_job(conn, advisor, job_id=job.job_id, user_id=job.user_id)
        job_queue_repo.mark_done(conn, job.job_id, outcome.delivery or "")
        logger.info("job %s finished: %s", job.job_id, outcome.status)
        _deliver(send, job, outcome.delivery or "")
    except Exception as exc:  # noqa: BLE001 - one bad job must not kill the worker
        max_retries = get_policies().max_job_retries
        if _is_retryable_error(exc) and job.attempts <= max_retries:
            logger.warning(
                "job %s transient failure (attempt %s/%s), requeuing: %s",
                job.job_id,
                job.attempts,
                max_retries,
                exc,
            )
            job_queue_repo.requeue_pending(conn, job.job_id, str(exc))
            return
        logger.error("job %s failed: %s", job.job_id, exc)
        job_queue_repo.mark_failed(conn, job.job_id, str(exc))


def serve_jobs(
    conn,
    advisor: Advisor,
    send: SendFn | None = None,
    *,
    once: bool = False,
    idle_seconds: float = DEFAULT_IDLE_SECONDS,
    on_idle_sleep: Callable[[float], None] = time.sleep,
) -> int:
    """Drain the job queue, running + delivering each planned job (§6B).

    ``once`` drains every currently-queued job and returns (handy for a CLI
    pass / tests); otherwise it loops forever, idling between empty polls, until
    interrupted. ``send``/``on_idle_sleep`` are injectable seams for tests.
    """
    logger.info("job worker started (once=%s)", once)
    while True:
        job = job_queue_repo.claim_next(conn)
        if job is None:
            if once:
                return 0  # queue drained
            on_idle_sleep(idle_seconds)
            continue
        _process_one(conn, advisor, send, job)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m app.cli.jobworker",
        description="Run planned jobs from the queue and deliver their results.",
    )
    parser.add_argument(
        "--db", type=Path, default=None, help="database file (default: data/app.db)"
    )
    parser.add_argument("--once", action="store_true", help="drain queued jobs, then exit")
    parser.add_argument(
        "-d", "--debug", action="store_true", help="stream logs (incl. model responses) to console"
    )
    args = parser.parse_args(argv)

    load_dotenv(REPO_ROOT / ".env", override=False)
    setup_run_logging("jobworker", console_level=logging.DEBUG if args.debug else logging.INFO)

    try:
        models = load_models_config()
    except (MissingCredentialError, KeyError, FileNotFoundError) as exc:
        print(f"[fail] configuration error: {exc}")
        return 1

    db_path = args.db or (REPO_ROOT / "data" / DEFAULT_DB_NAME)
    conn = connect(db_path)
    try:
        migrate(conn)
        # CLI mode delivers via the Telegram bot when configured; otherwise it
        # just runs jobs and records results (visible on the dashboard).
        send: SendFn | None = None
        settings = get_settings()
        token = settings.telegram_bot_token
        if token is not None and token.reveal().strip():
            from app.channels.telegram import TelegramAdapter

            send = TelegramAdapter(token, webhook_secret=settings.telegram_webhook_secret).send

        from app.cli.ask import build_resolver

        advisor = Advisor(resolve_provider=build_resolver(models), conn=conn)
        print("[ok]   Job worker running. Press Ctrl-C to stop.")
        return serve_jobs(conn, advisor, send, once=args.once)
    except KeyboardInterrupt:
        print("\n[ok]   Stopped.")
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
