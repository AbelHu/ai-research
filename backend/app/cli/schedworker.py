"""Scheduler worker — runs periodic, deterministic schedules (design-spec §9, §11).

Drains the ``schedules`` table: it claims each **due** row (``next_run_at`` has
passed), runs the handler registered for its ``kind``, and advances
``next_run_at`` by the row's interval (`app.scheduling`). The standing default is
the **Librarian's daily memory-maintenance sweep**; the dispatch table is
extensible for future proactive generators (digests, data products).

This lane is pure local DB work — no model, no chat — so it runs even without a
configured bot, and **one failing schedule never stops the loop**.

Run from the ``backend/`` directory:

    python -m app.cli.schedworker          # run due schedules, loop; Ctrl-C to stop
    python -m app.cli.schedworker --once   # run everything due now, then exit
"""

from __future__ import annotations

import argparse
import logging
import sqlite3
import sys
import time
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv

from app.config.settings import REPO_ROOT
from app.roles import librarian
from app.runlog import setup_run_logging
from app.scheduling import next_run_after
from app.storage.db import connect
from app.storage.migrations import migrate
from app.storage.repos import schedules as schedules_repo

logger = logging.getLogger("app.cli.schedworker")

DEFAULT_DB_NAME = "app.db"
DEFAULT_POLL_SECONDS = 60.0

# The standing default schedules run by the Librarian.
MEMORY_MAINTENANCE = "memory_maintenance"
LIBRARY_COMPACTION = "library_compaction"
_DEFAULT_SCHEDULES: tuple[tuple[str, str], ...] = (
    (MEMORY_MAINTENANCE, "@daily"),
    (LIBRARY_COMPACTION, "@daily"),
)


def _run_memory_maintenance(conn: sqlite3.Connection) -> str:
    res = librarian.run_memory_maintenance(conn)
    return (
        f"dropped={len(res.dropped)} archived={len(res.archived)} "
        f"promoted={len(res.promoted)} consolidated={len(res.consolidated)}"
    )


def _run_library_compaction(conn: sqlite3.Connection) -> str:
    res = librarian.compact_cold_library(conn)
    return f"compacted={len(res.compacted)} bytes_saved={res.bytes_saved}"


# kind -> handler(conn) -> a short summary string for the log.
HANDLERS: dict[str, Callable[[sqlite3.Connection], str]] = {
    MEMORY_MAINTENANCE: _run_memory_maintenance,
    LIBRARY_COMPACTION: _run_library_compaction,
}


def ensure_default_schedules(conn: sqlite3.Connection, *, now: datetime | None = None) -> None:
    """Seed the standing default schedules (idempotent: at most one row per kind)."""
    moment = now or datetime.now(tz=timezone.utc)
    for kind, spec in _DEFAULT_SCHEDULES:
        if schedules_repo.get_by_kind(conn, kind) is None:
            schedules_repo.create_schedule(conn, kind=kind, schedule_cron=spec, next_run_at=moment)
            logger.info("seeded default schedule %r (%s)", kind, spec)


def run_due_schedules(conn: sqlite3.Connection, *, now: datetime | None = None) -> int:
    """Run every currently-due schedule once; return how many were due."""
    moment = now or datetime.now(tz=timezone.utc)
    due = schedules_repo.list_due(conn, now=moment)
    for sched in due:
        handler = HANDLERS.get(sched.kind)
        if handler is None:
            logger.warning("schedule %s: no handler for kind %r — skipping", sched.id, sched.kind)
        else:
            try:
                summary = handler(conn)
                logger.info("schedule %s (%s) ran: %s", sched.id, sched.kind, summary)
            except Exception as exc:  # noqa: BLE001 - one bad schedule must not stop the loop
                logger.error("schedule %s (%s) failed: %s", sched.id, sched.kind, exc)
        # Advance next_run_at regardless, so a due row doesn't re-fire every poll.
        schedules_repo.mark_run(
            conn,
            sched.id,
            last_run_at=moment,
            next_run_at=next_run_after(sched.schedule_cron, moment),
        )
    return len(due)


def serve_schedules(
    conn: sqlite3.Connection,
    *,
    once: bool = False,
    poll_seconds: float = DEFAULT_POLL_SECONDS,
    on_idle_sleep: Callable[[float], None] = time.sleep,
) -> int:
    """Seed defaults, then run due schedules — once, or loop forever (the service)."""
    ensure_default_schedules(conn)
    logger.info("scheduler started (once=%s)", once)
    while True:
        run_due_schedules(conn)
        if once:
            return 0
        on_idle_sleep(poll_seconds)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m app.cli.schedworker",
        description="Run periodic schedules (e.g. the Librarian's daily memory maintenance).",
    )
    parser.add_argument(
        "--db", type=Path, default=None, help="database file (default: data/app.db)"
    )
    parser.add_argument("--once", action="store_true", help="run everything due now, then exit")
    parser.add_argument(
        "-d", "--debug", action="store_true", help="stream logs to console"
    )
    args = parser.parse_args(argv)

    load_dotenv(REPO_ROOT / ".env", override=False)
    setup_run_logging("schedworker", console_level=logging.DEBUG if args.debug else logging.INFO)

    db_path = args.db or (REPO_ROOT / "data" / DEFAULT_DB_NAME)
    conn = connect(db_path)
    try:
        migrate(conn)
        print("[ok]   Scheduler running. Press Ctrl-C to stop.")
        return serve_schedules(conn, once=args.once)
    except KeyboardInterrupt:
        print("\n[ok]   Stopped.")
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
