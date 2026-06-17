"""Requests & jobs repository (design-spec §6C, §9; implementation-plan T1.9).

Typed create/get/list helpers plus the canonical request-**code** generator.

The code is ``YYYYMMDDHHmmSS`` with a ``-NN`` suffix appended **only** on a
same-second collision (design-spec §9.2 / §6C). Because the code *is* the
library folder name, it must stay unique and filesystem-safe, so the tie-break
suffix is part of the code itself (not a folder-only rename).
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime

# Width of the zero-padded same-second tie-break suffix (e.g. "-01").
_SUFFIX_WIDTH = 2

# `requests.status` value meaning "this request is waiting on the user's reply"
# — set when the Analyzer asks for clarification or a job's plan is declined, so
# the PM can thread an unprefixed follow-up back to it instead of minting a new
# request (design-spec §6C continuity).
AWAITING_STATUS = "awaiting_user"


@dataclass(frozen=True)
class Request:
    id: int
    code: str
    title: str | None
    status: str | None
    user_id: int | None
    session_id: int | None
    improves_request_id: int | None
    state: str
    created_at: str

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> Request:
        return cls(
            id=row["id"],
            code=row["code"],
            title=row["title"],
            status=row["status"],
            user_id=row["user_id"],
            session_id=row["session_id"],
            improves_request_id=row["improves_request_id"],
            state=row["state"],
            created_at=row["created_at"],
        )


@dataclass(frozen=True)
class Job:
    id: int
    request_id: int
    kind: str
    clarity: str | None
    complexity: str | None
    folder_path: str | None
    paused: bool
    created_at: str

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> Job:
        return cls(
            id=row["id"],
            request_id=row["request_id"],
            kind=row["kind"],
            clarity=row["clarity"],
            complexity=row["complexity"],
            folder_path=row["folder_path"],
            paused=bool(row["paused"]),
            created_at=row["created_at"],
        )


def next_code(conn: sqlite3.Connection, *, now: datetime | None = None) -> str:
    """Return the next unused request code for the current second.

    First request in a given second gets the bare ``YYYYMMDDHHmmSS``; a second
    request in the same second gets ``...-01``, the next ``...-02``, and so on.
    """
    base = (now or datetime.now()).strftime("%Y%m%d%H%M%S")
    rows = conn.execute(
        "SELECT code FROM requests WHERE code = ? OR code LIKE ?",
        (base, f"{base}-%"),
    ).fetchall()
    if not rows:
        return base

    max_suffix = 0
    for (code,) in (tuple(r) for r in rows):
        if code.startswith(f"{base}-"):
            tail = code[len(base) + 1 :]
            if tail.isdigit():
                max_suffix = max(max_suffix, int(tail))
    return f"{base}-{max_suffix + 1:0{_SUFFIX_WIDTH}d}"


def create_request(
    conn: sqlite3.Connection,
    *,
    title: str | None = None,
    status: str | None = None,
    user_id: int | None = None,
    session_id: int | None = None,
    improves_request_id: int | None = None,
    now: datetime | None = None,
) -> Request:
    """Insert a request, minting its canonical code. Returns the stored row."""
    # Retry once on the (single-process-unlikely) same-second UNIQUE race.
    for attempt in range(2):
        code = next_code(conn, now=now)
        try:
            with conn:
                cur = conn.execute(
                    "INSERT INTO requests "
                    "(code, title, status, user_id, session_id, improves_request_id) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (code, title, status, user_id, session_id, improves_request_id),
                )
            request_id = int(cur.lastrowid)
            break
        except sqlite3.IntegrityError:
            if attempt == 1:
                raise
    got = get_request(conn, request_id)
    assert got is not None  # just inserted
    return got


def get_request(conn: sqlite3.Connection, request_id: int) -> Request | None:
    row = conn.execute("SELECT * FROM requests WHERE id = ?", (request_id,)).fetchone()
    return Request.from_row(row) if row else None


def get_request_by_code(conn: sqlite3.Connection, code: str) -> Request | None:
    row = conn.execute("SELECT * FROM requests WHERE code = ?", (code,)).fetchone()
    return Request.from_row(row) if row else None


def set_request_state(conn: sqlite3.Connection, request_id: int, state: str) -> Request:
    """Set a request's lifecycle state (active | archived | dropped) (§9.1)."""
    if state not in {"active", "archived", "dropped"}:
        raise ValueError(f"invalid request state: {state!r}")
    with conn:
        conn.execute("UPDATE requests SET state = ? WHERE id = ?", (state, request_id))
    updated = get_request(conn, request_id)
    assert updated is not None
    return updated


def set_request_status(conn: sqlite3.Connection, request_id: int, status: str | None) -> Request:
    """Set (or clear, with ``None``) a request's free-form ``status`` (§6C).

    Distinct from ``state`` (the active/archived/dropped lifecycle): ``status``
    flags transient progress — notably :data:`AWAITING_STATUS`, set when the
    request is waiting on the user's reply so a follow-up can be threaded back to
    it.
    """
    with conn:
        conn.execute("UPDATE requests SET status = ? WHERE id = ?", (status, request_id))
    updated = get_request(conn, request_id)
    assert updated is not None
    return updated


def get_latest_awaiting_request(conn: sqlite3.Connection, user_id: int) -> Request | None:
    """Return the user's most recent **active** request awaiting their reply.

    Used by the PM to thread an unprefixed follow-up (no ``/req`` marker) back to
    the request that asked the user for clarification or a declined plan, instead
    of minting a brand-new request and losing the thread (§6C continuity).
    """
    row = conn.execute(
        "SELECT * FROM requests "
        "WHERE user_id = ? AND state = 'active' AND status = ? "
        "ORDER BY id DESC LIMIT 1",
        (user_id, AWAITING_STATUS),
    ).fetchone()
    return Request.from_row(row) if row else None


def get_latest_active_request(
    conn: sqlite3.Connection,
    user_id: int,
    *,
    exclude_request_id: int | None = None,
) -> Request | None:
    """Return the user's most recent **active** request (the "current thread").

    Unlike :func:`get_latest_awaiting_request` this ignores ``status`` — it's the
    last thing the user worked on, used to (a) best-guess whether a new message
    continues it and (b) build the conversation context so references like "the
    plan"/"that URL" resolve to the prior turn (§6C). ``exclude_request_id`` skips
    a request (e.g. the one currently being routed) to find the **prior** turn.
    """
    if exclude_request_id is None:
        row = conn.execute(
            "SELECT * FROM requests WHERE user_id = ? AND state = 'active' "
            "ORDER BY id DESC LIMIT 1",
            (user_id,),
        ).fetchone()
    else:
        row = conn.execute(
            "SELECT * FROM requests WHERE user_id = ? AND state = 'active' AND id != ? "
            "ORDER BY id DESC LIMIT 1",
            (user_id, exclude_request_id),
        ).fetchone()
    return Request.from_row(row) if row else None


def list_requests(
    conn: sqlite3.Connection,
    *,
    state: str | None = None,
    limit: int = 100,
) -> list[Request]:
    """List requests newest-first, optionally filtered by lifecycle state."""
    if state is None:
        rows = conn.execute("SELECT * FROM requests ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM requests WHERE state = ? ORDER BY id DESC LIMIT ?",
            (state, limit),
        ).fetchall()
    return [Request.from_row(r) for r in rows]


def create_job(
    conn: sqlite3.Connection,
    *,
    request_id: int,
    kind: str,
    clarity: str | None = None,
    complexity: str | None = None,
    folder_path: str | None = None,
) -> Job:
    """Insert the job for a request (one per request). Returns the stored row."""
    with conn:
        cur = conn.execute(
            "INSERT INTO jobs (request_id, kind, clarity, complexity, folder_path) "
            "VALUES (?, ?, ?, ?, ?)",
            (request_id, kind, clarity, complexity, folder_path),
        )
    got = get_job(conn, int(cur.lastrowid))
    assert got is not None  # just inserted
    return got


def get_job(conn: sqlite3.Connection, job_id: int) -> Job | None:
    row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
    return Job.from_row(row) if row else None


def get_job_for_request(conn: sqlite3.Connection, request_id: int) -> Job | None:
    row = conn.execute(
        "SELECT * FROM jobs WHERE request_id = ? ORDER BY id LIMIT 1", (request_id,)
    ).fetchone()
    return Job.from_row(row) if row else None


def set_job_kind(conn: sqlite3.Connection, job_id: int, kind: str) -> Job:
    """Re-classify a job's ``kind`` (e.g. promote ``ask`` → ``task``/``feature``).

    Used when the Junior Worker can't answer a simple ask and the work is handed
    back to the Analyzer for planning (§6A): the job is promoted to a complex
    kind so it follows the plan → sign-off → runner path. Returns the updated row.
    """
    with conn:
        conn.execute("UPDATE jobs SET kind = ? WHERE id = ?", (kind, job_id))
    got = get_job(conn, job_id)
    assert got is not None  # caller holds a live job id
    return got


def set_job_paused(conn: sqlite3.Connection, job_id: int, paused: bool) -> Job:
    """Set/clear a job's durable ``paused`` flag (design-spec §6B; plan T6.7).

    ``jobs.paused`` is the durable source of truth that suspends the whole job;
    a per-job ``asyncio.Event`` (see `app.roles.jobcontrol`) is the live signal.
    Returns the updated job.
    """
    with conn:
        if paused:
            conn.execute(
                "UPDATE jobs SET paused = 1, paused_at = datetime('now') WHERE id = ?",
                (job_id,),
            )
        else:
            conn.execute(
                "UPDATE jobs SET paused = 0, paused_at = NULL WHERE id = ?",
                (job_id,),
            )
    updated = get_job(conn, job_id)
    assert updated is not None
    return updated


def add_request_detail(
    conn: sqlite3.Connection,
    *,
    request_id: int,
    content: str,
    source: str = "user",
    routed_by: str = "pm",
    confidence: float | None = None,
    reroute_count: int = 0,
) -> int:
    """Append a detail to a request (the §6C "append"). Returns the new row id.

    A detail's lifecycle (active/rejected/reassigned) is tracked here, not on
    `requests` — so an Analyzer reject mutates *this* row, never a request flag.
    """
    with conn:
        cur = conn.execute(
            "INSERT INTO request_details "
            "(request_id, content, source, routed_by, confidence, reroute_count) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (request_id, content, source, routed_by, confidence, reroute_count),
        )
    return int(cur.lastrowid)


def list_request_details(
    conn: sqlite3.Connection,
    request_id: int,
    *,
    state: str | None = None,
) -> list[sqlite3.Row]:
    """Return a request's details in creation order, optionally filtered by state."""
    if state is None:
        return conn.execute(
            "SELECT * FROM request_details WHERE request_id = ? ORDER BY id",
            (request_id,),
        ).fetchall()
    return conn.execute(
        "SELECT * FROM request_details WHERE request_id = ? AND state = ? ORDER BY id",
        (request_id, state),
    ).fetchall()
