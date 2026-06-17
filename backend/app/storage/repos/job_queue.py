"""Job-execution queue repository (service slice B2; design-spec §6B).

The durable hand-off between the **gateway** (which classifies a message into a
planned job) and the **background job worker** (which runs it end-to-end and
delivers the result). One row per job; ``enqueue`` is idempotent, and
``claim_next`` atomically moves the oldest ``pending`` job to ``running`` so a
job is never run twice.

Functions take ``conn`` first and wrap writes in ``with conn:`` (the repo
convention). Rows are returned as frozen `QueuedJob` dataclasses.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

# Lifecycle of a queued job. ``pending`` → ``running`` (claimed) → ``done`` |
# ``failed``. Transient failures are requeued to ``pending`` by the worker
# (bounded by ``max_job_retries``); a non-transient failure stays ``failed`` for
# inspection.
PENDING = "pending"
RUNNING = "running"
DONE = "done"
FAILED = "failed"


@dataclass(frozen=True)
class QueuedJob:
    """A ``job_queue`` row: a planned job awaiting (or under) background execution."""

    job_id: int
    status: str
    channel: str | None
    chat_id: str | None
    reply_to_message_id: str | None
    user_id: int | None
    result: str | None
    error: str | None
    attempts: int
    created_at: str
    updated_at: str | None

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> QueuedJob:
        return cls(
            job_id=row["job_id"],
            status=row["status"],
            channel=row["channel"],
            chat_id=row["chat_id"],
            reply_to_message_id=row["reply_to_message_id"],
            user_id=row["user_id"],
            result=row["result"],
            error=row["error"],
            attempts=row["attempts"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )


def enqueue(
    conn: sqlite3.Connection,
    job_id: int,
    *,
    channel: str | None = None,
    chat_id: str | None = None,
    reply_to_message_id: str | None = None,
    user_id: int | None = None,
) -> QueuedJob:
    """Enqueue a planned job for background execution (idempotent per job).

    ``INSERT OR IGNORE`` keeps a re-enqueue (e.g. a retried inbound) from
    duplicating the row or resetting an in-flight job. The delivery coordinates
    address the follow-up reply back to the originating chat.
    """
    with conn:
        conn.execute(
            "INSERT OR IGNORE INTO job_queue "
            "(job_id, channel, chat_id, reply_to_message_id, user_id) "
            "VALUES (?, ?, ?, ?, ?)",
            (job_id, channel, chat_id, reply_to_message_id, user_id),
        )
    got = get(conn, job_id)
    assert got is not None  # just inserted (or already present)
    return got


def get(conn: sqlite3.Connection, job_id: int) -> QueuedJob | None:
    row = conn.execute("SELECT * FROM job_queue WHERE job_id = ?", (job_id,)).fetchone()
    return QueuedJob.from_row(row) if row else None


def claim_next(conn: sqlite3.Connection) -> QueuedJob | None:
    """Atomically claim the oldest ``pending`` job, moving it to ``running``.

    Returns the claimed job (now ``running``) or ``None`` when nothing is
    pending. The guarded ``UPDATE ... WHERE status='pending'`` makes the claim
    safe even if two workers race — only one wins the row.
    """
    row = conn.execute(
        "SELECT job_id FROM job_queue WHERE status = ? ORDER BY job_id LIMIT 1",
        (PENDING,),
    ).fetchone()
    if row is None:
        return None
    job_id = row["job_id"]
    with conn:
        cur = conn.execute(
            "UPDATE job_queue SET status = ?, attempts = attempts + 1, "
            "updated_at = datetime('now') WHERE job_id = ? AND status = ?",
            (RUNNING, job_id, PENDING),
        )
    if cur.rowcount == 0:
        return None  # lost the race to another worker
    return get(conn, job_id)


def mark_done(conn: sqlite3.Connection, job_id: int, result: str) -> None:
    """Mark a claimed job finished, storing the delivered result text."""
    with conn:
        conn.execute(
            "UPDATE job_queue SET status = ?, result = ?, updated_at = datetime('now') "
            "WHERE job_id = ?",
            (DONE, result, job_id),
        )


def mark_failed(conn: sqlite3.Connection, job_id: int, error: str) -> None:
    """Mark a claimed job failed, storing the error for inspection (no retry)."""
    with conn:
        conn.execute(
            "UPDATE job_queue SET status = ?, error = ?, updated_at = datetime('now') "
            "WHERE job_id = ?",
            (FAILED, error, job_id),
        )


def requeue_pending(conn: sqlite3.Connection, job_id: int, error: str) -> None:
    """Return a running job to ``pending`` after a retryable transient failure."""
    with conn:
        conn.execute(
            "UPDATE job_queue SET status = ?, error = ?, updated_at = datetime('now') "
            "WHERE job_id = ?",
            (PENDING, error, job_id),
        )


def list_by_status(conn: sqlite3.Connection, status: str) -> list[QueuedJob]:
    """Return queued jobs in a given status, oldest first (for status/tests)."""
    rows = conn.execute(
        "SELECT * FROM job_queue WHERE status = ? ORDER BY job_id", (status,)
    ).fetchall()
    return [QueuedJob.from_row(r) for r in rows]


def count_pending(conn: sqlite3.Connection) -> int:
    """How many jobs are awaiting a worker (for status / the dashboard)."""
    return int(
        conn.execute("SELECT COUNT(*) FROM job_queue WHERE status = ?", (PENDING,)).fetchone()[0]
    )
