"""AI-call audit repository (design-spec §7, §9; implementation-plan T3.3).

Every advisor model call writes one row here: which role/model/template ran, a
**reference** (content hash) of the prompt + response, token/latency metrics and
the final ``validation_status`` (`valid` | `repaired` | `fallback` | `failed`).

Refs are SHA-256 digests, not raw text — the audit trail never stores the API
token (§12) and avoids persisting prompt/response bodies inline at this layer.
"""

from __future__ import annotations

import hashlib
import sqlite3


def content_ref(text: str) -> str:
    """A stable, non-reversible reference for prompt/response audit columns."""
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


def record_ai_call(
    conn: sqlite3.Connection,
    *,
    request_id: int,
    role: str | None = None,
    model_id: str | None = None,
    template: str | None = None,
    prompt_ref: str | None = None,
    response_ref: str | None = None,
    tokens: int | None = None,
    latency_ms: int | None = None,
    validation_status: str | None = None,
    job_id: int | None = None,
    role_message_id: int | None = None,
    step_id: int | None = None,
) -> int:
    """Insert an audit row for a single model call. Returns the new id."""
    with conn:
        cur = conn.execute(
            "INSERT INTO ai_calls "
            "(request_id, role_message_id, job_id, step_id, role, model_id, "
            " template, prompt_ref, response_ref, tokens, latency_ms, validation_status) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                request_id,
                role_message_id,
                job_id,
                step_id,
                role,
                model_id,
                template,
                prompt_ref,
                response_ref,
                tokens,
                latency_ms,
                validation_status,
            ),
        )
    return int(cur.lastrowid)


def get_ai_call(conn: sqlite3.Connection, ai_call_id: int) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM ai_calls WHERE id = ?", (ai_call_id,)).fetchone()


def list_ai_calls(conn: sqlite3.Connection, request_id: int) -> list[sqlite3.Row]:
    """Return a request's audit rows in creation order."""
    return conn.execute(
        "SELECT * FROM ai_calls WHERE request_id = ? ORDER BY id",
        (request_id,),
    ).fetchall()
