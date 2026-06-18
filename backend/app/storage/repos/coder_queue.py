"""Coder-queue repository — the dedicated codegen lane (P4; design-spec §5/§6B).

A feature job hands skill generation off to a separate, privileged **coder
worker** (fs-write + subprocess sandbox). This queue is the decoupling seam: the
main pipeline enqueues one *coding request* per feature job; the coder worker
consumes it, runs the agentic Coder loop, and records the result here.

The row is the transport-agnostic **contract** — inputs (``goal`` + linkage +
delivery coords) and outputs (``status`` + ``skill_modules`` + ``validation`` +
``error``) — so the coding subsystem can later move behind a remote API/service
without changing callers. ``job_id`` is the primary key, so ``enqueue`` is
idempotent per feature job.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field

# Lifecycle: ``pending`` → ``running`` (claimed) → ``done`` | ``failed``.
PENDING = "pending"
RUNNING = "running"
DONE = "done"
FAILED = "failed"


@dataclass(frozen=True)
class CoderJob:
    """A ``coder_queue`` row: one feature job's coding request + its result."""

    job_id: int
    request_id: int
    job_code: str
    goal: str
    status: str
    channel: str | None
    chat_id: str | None
    reply_to_message_id: str | None
    user_id: int | None
    error: str | None
    attempts: int
    created_at: str
    updated_at: str | None
    skill_modules: list[str] = field(default_factory=list)
    validation: dict | None = None

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> CoderJob:
        return cls(
            job_id=row["job_id"],
            request_id=row["request_id"],
            job_code=row["job_code"],
            goal=row["goal"],
            status=row["status"],
            channel=row["channel"],
            chat_id=row["chat_id"],
            reply_to_message_id=row["reply_to_message_id"],
            user_id=row["user_id"],
            error=row["error"],
            attempts=row["attempts"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            skill_modules=json.loads(row["skill_modules"]) if row["skill_modules"] else [],
            validation=json.loads(row["validation"]) if row["validation"] else None,
        )


def enqueue(
    conn: sqlite3.Connection,
    *,
    job_id: int,
    request_id: int,
    job_code: str,
    goal: str,
    channel: str | None = None,
    chat_id: str | None = None,
    reply_to_message_id: str | None = None,
    user_id: int | None = None,
) -> CoderJob:
    """Enqueue a feature job's coding request (idempotent per ``job_id``)."""
    with conn:
        conn.execute(
            "INSERT OR IGNORE INTO coder_queue "
            "(job_id, request_id, job_code, goal, channel, chat_id, reply_to_message_id, user_id) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (job_id, request_id, job_code, goal, channel, chat_id, reply_to_message_id, user_id),
        )
    got = get(conn, job_id)
    assert got is not None  # just inserted (or already present)
    return got


def get(conn: sqlite3.Connection, job_id: int) -> CoderJob | None:
    row = conn.execute("SELECT * FROM coder_queue WHERE job_id = ?", (job_id,)).fetchone()
    return CoderJob.from_row(row) if row else None


def claim_next(conn: sqlite3.Connection) -> CoderJob | None:
    """Atomically claim the oldest ``pending`` coding request → ``running``."""
    row = conn.execute(
        "SELECT job_id FROM coder_queue WHERE status = ? ORDER BY job_id LIMIT 1", (PENDING,)
    ).fetchone()
    if row is None:
        return None
    job_id = row["job_id"]
    with conn:
        cur = conn.execute(
            "UPDATE coder_queue SET status = ?, attempts = attempts + 1, "
            "updated_at = datetime('now') WHERE job_id = ? AND status = ?",
            (RUNNING, job_id, PENDING),
        )
    if cur.rowcount == 0:
        return None  # lost the race
    return get(conn, job_id)


def mark_done(
    conn: sqlite3.Connection,
    job_id: int,
    *,
    skill_modules: list[str],
    validation: dict | None = None,
) -> None:
    """Record a successful coding request: the promoted (inert) skill modules."""
    with conn:
        conn.execute(
            "UPDATE coder_queue SET status = ?, skill_modules = ?, validation = ?, error = NULL, "
            "updated_at = datetime('now') WHERE job_id = ?",
            (
                DONE,
                json.dumps(list(skill_modules)),
                json.dumps(validation) if validation is not None else None,
                job_id,
            ),
        )


def mark_failed(
    conn: sqlite3.Connection, job_id: int, error: str, *, validation: dict | None = None
) -> None:
    """Record a coding request that couldn't produce a validated bundle."""
    with conn:
        conn.execute(
            "UPDATE coder_queue SET status = ?, error = ?, validation = ?, "
            "updated_at = datetime('now') WHERE job_id = ?",
            (FAILED, error, json.dumps(validation) if validation is not None else None, job_id),
        )


def list_by_status(conn: sqlite3.Connection, status: str) -> list[CoderJob]:
    rows = conn.execute(
        "SELECT * FROM coder_queue WHERE status = ? ORDER BY job_id", (status,)
    ).fetchall()
    return [CoderJob.from_row(r) for r in rows]


def count_pending(conn: sqlite3.Connection) -> int:
    return int(
        conn.execute("SELECT COUNT(*) FROM coder_queue WHERE status = ?", (PENDING,)).fetchone()[0]
    )
