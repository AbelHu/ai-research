"""Schedules repository — periodic, deterministic work (design-spec §9, §11).

Rows in the ``schedules`` table drive recurring work. The standing default is
the **Librarian's daily memory-maintenance sweep** (TTL drop/archive/promote);
the same table is designed to host future proactive generators (digests, data
products). Each row carries a ``kind`` (what to run), a ``schedule_cron``
interval spec (how often — see :mod:`app.scheduling`), and ``next_run_at`` (when
it is next due). The scheduler worker (`app.cli.schedworker`) claims due rows,
runs the matching handler, and advances ``next_run_at``.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone


def _utcnow() -> datetime:
    return datetime.now(tz=timezone.utc)


def _fmt(dt: datetime) -> str:
    """Format as the SQLite ``datetime('now')`` shape ('YYYY-MM-DD HH:MM:SS', UTC)."""
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


@dataclass(frozen=True)
class Schedule:
    """A ``schedules`` row: a recurring task + when it is next due."""

    id: int
    kind: str
    schedule_cron: str | None
    params: dict
    enabled: bool
    created_by_request: int | None
    last_run_at: str | None
    next_run_at: str | None
    created_at: str

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> Schedule:
        return cls(
            id=row["id"],
            kind=row["kind"],
            schedule_cron=row["schedule_cron"],
            params=json.loads(row["params_json"]) if row["params_json"] else {},
            enabled=bool(row["enabled"]),
            created_by_request=row["created_by_request"],
            last_run_at=row["last_run_at"],
            next_run_at=row["next_run_at"],
            created_at=row["created_at"],
        )


def create_schedule(
    conn: sqlite3.Connection,
    *,
    kind: str,
    schedule_cron: str | None,
    params: dict | None = None,
    enabled: bool = True,
    next_run_at: datetime | str | None = None,
    created_by_request: int | None = None,
) -> Schedule:
    """Insert a schedule; ``next_run_at`` may be a datetime (UTC) or a string."""
    next_at = _fmt(next_run_at) if isinstance(next_run_at, datetime) else next_run_at
    with conn:
        cur = conn.execute(
            "INSERT INTO schedules "
            "(kind, schedule_cron, params_json, enabled, created_by_request, next_run_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                kind,
                schedule_cron,
                json.dumps(params or {}),
                1 if enabled else 0,
                created_by_request,
                next_at,
            ),
        )
    got = get_schedule(conn, int(cur.lastrowid))
    assert got is not None  # just inserted
    return got


def get_schedule(conn: sqlite3.Connection, schedule_id: int) -> Schedule | None:
    row = conn.execute("SELECT * FROM schedules WHERE id = ?", (schedule_id,)).fetchone()
    return Schedule.from_row(row) if row else None


def get_by_kind(conn: sqlite3.Connection, kind: str) -> Schedule | None:
    """The first schedule of a ``kind`` (used for singletons like memory_maintenance)."""
    row = conn.execute(
        "SELECT * FROM schedules WHERE kind = ? ORDER BY id LIMIT 1", (kind,)
    ).fetchone()
    return Schedule.from_row(row) if row else None


def list_schedules(conn: sqlite3.Connection, *, enabled_only: bool = False) -> list[Schedule]:
    sql = "SELECT * FROM schedules"
    if enabled_only:
        sql += " WHERE enabled = 1"
    sql += " ORDER BY id"
    return [Schedule.from_row(r) for r in conn.execute(sql).fetchall()]


def list_due(conn: sqlite3.Connection, *, now: datetime | None = None) -> list[Schedule]:
    """Enabled schedules whose ``next_run_at`` has passed (or was never set)."""
    moment = _fmt(now or _utcnow())
    rows = conn.execute(
        "SELECT * FROM schedules WHERE enabled = 1 "
        "AND (next_run_at IS NULL OR next_run_at <= ?) ORDER BY id",
        (moment,),
    ).fetchall()
    return [Schedule.from_row(r) for r in rows]


def mark_run(
    conn: sqlite3.Connection,
    schedule_id: int,
    *,
    last_run_at: datetime,
    next_run_at: datetime,
) -> None:
    """Record a run and when the schedule is next due."""
    with conn:
        conn.execute(
            "UPDATE schedules SET last_run_at = ?, next_run_at = ? WHERE id = ?",
            (_fmt(last_run_at), _fmt(next_run_at), schedule_id),
        )


def set_enabled(conn: sqlite3.Connection, schedule_id: int, enabled: bool) -> None:
    with conn:
        conn.execute(
            "UPDATE schedules SET enabled = ? WHERE id = ?",
            (1 if enabled else 0, schedule_id),
        )
