"""Role-message (envelope) repository (design-spec §6D, §9; implementation-plan T4.1).

The `role_messages` table is the inter-role queue + durable log: one row per
hand-off, routed by `action` and chained by `causation_id` (who-asked-whom). The
flow is recoverable (rebuild in-flight hand-offs after a crash) and auditable
(the causation chain reconstructs the trace).
"""

from __future__ import annotations

import json
import sqlite3

from app.roles.envelope import Action, Role, RoleMessage


def record_envelope(
    conn: sqlite3.Connection,
    msg: RoleMessage,
    *,
    causation_id: int | None = None,
) -> int:
    """Persist an envelope, returning its new DB id.

    An explicit ``causation_id`` overrides the one carried on ``msg`` (the
    control loop passes the id of the message this one answers).
    """
    cause = causation_id if causation_id is not None else msg.causation_id
    with conn:
        cur = conn.execute(
            "INSERT INTO role_messages "
            "(request_id, job_id, from_role, to_role, action, payload_json, "
            " template, status, causation_id) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                msg.request_id,
                msg.job_id,
                msg.from_role.value,
                msg.to_role.value,
                msg.action.value,
                json.dumps(msg.payload),
                msg.template,
                msg.status,
                cause,
            ),
        )
    return int(cur.lastrowid)


def envelope_from_row(row: sqlite3.Row) -> RoleMessage:
    """Rebuild a `RoleMessage` from a stored row (round-trips `record_envelope`)."""
    return RoleMessage(
        id=row["id"],
        request_id=row["request_id"],
        job_id=row["job_id"],
        from_role=Role(row["from_role"]),
        to_role=Role(row["to_role"]),
        action=Action(row["action"]),
        payload=json.loads(row["payload_json"]) if row["payload_json"] else {},
        template=row["template"],
        status=row["status"],
        causation_id=row["causation_id"],
        created_at=row["created_at"],
    )


def get_role_message(conn: sqlite3.Connection, message_id: int) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM role_messages WHERE id = ?", (message_id,)).fetchone()


def list_role_messages(conn: sqlite3.Connection, request_id: int) -> list[sqlite3.Row]:
    """Return a request's envelopes in creation order (the trace)."""
    return conn.execute(
        "SELECT * FROM role_messages WHERE request_id = ? ORDER BY id",
        (request_id,),
    ).fetchall()


def get_last_answer_text(conn: sqlite3.Connection, request_id: int) -> str | None:
    """Return the most recent **answer text** delivered for a request, if any.

    Scans the request's envelopes newest-first for one whose payload carries an
    ``answer`` object (the Junior's ``ask_done`` / the PM ``deliver`` hand-off)
    and returns its ``answer`` string. Used to build the conversation context so
    a follow-up can resolve references to "the previous answer" (§6C). Returns
    ``None`` when the request has produced no answer yet.
    """
    rows = conn.execute(
        "SELECT payload_json FROM role_messages WHERE request_id = ? ORDER BY id DESC",
        (request_id,),
    ).fetchall()
    for row in rows:
        if not row["payload_json"]:
            continue
        payload = json.loads(row["payload_json"])
        answer = payload.get("answer")
        if isinstance(answer, dict) and answer.get("answer"):
            return str(answer["answer"])
    return None


def update_status(conn: sqlite3.Connection, message_id: int, status: str) -> None:
    with conn:
        conn.execute(
            "UPDATE role_messages SET status = ? WHERE id = ?",
            (status, message_id),
        )
