"""Steps repository (design-spec §8.6; implementation-plan T2.4).

A **step** is one row per skill invocation — the recorded "process". The skill
runtime writes one here after every successful `execute()` so the work an AI
suggestion produced is fully auditable. ``idx`` is a per-job sequence number.
"""

from __future__ import annotations

import sqlite3


def record_step(
    conn: sqlite3.Connection,
    *,
    job_id: int,
    skill_name: str,
    status: str,
    params_json: str | None = None,
    result_json: str | None = None,
    provenance_json: str | None = None,
    plan_task_id: int | None = None,
    started_at: str | None = None,
    ended_at: str | None = None,
) -> int:
    """Append a step to a job's process log. Returns the new step id.

    ``idx`` is assigned as ``MAX(idx) + 1`` within the job so steps stay ordered.
    """
    with conn:
        (next_idx,) = conn.execute(
            "SELECT COALESCE(MAX(idx), -1) + 1 FROM steps WHERE job_id = ?",
            (job_id,),
        ).fetchone()
        cur = conn.execute(
            "INSERT INTO steps "
            "(job_id, plan_task_id, idx, skill_name, params_json, status, "
            " result_json, provenance_json, started_at, ended_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                job_id,
                plan_task_id,
                next_idx,
                skill_name,
                params_json,
                status,
                result_json,
                provenance_json,
                started_at,
                ended_at,
            ),
        )
    return int(cur.lastrowid)


def list_steps(conn: sqlite3.Connection, job_id: int) -> list[sqlite3.Row]:
    """Return a job's steps in execution order."""
    return conn.execute(
        "SELECT * FROM steps WHERE job_id = ? ORDER BY idx",
        (job_id,),
    ).fetchall()
