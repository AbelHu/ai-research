"""Audit-log repository (design-spec §9, §10.1; implementation-plan T7.4).

A tiny append-only writer for the ``audit_log`` table — the durable record of
security-relevant deterministic events (e.g. the gateway **refusing an unpaired
sender**, §10.1). Payload is stored as JSON; never put secrets here (§12).
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass

Actor = str  # "system" | "ai" | "user" | "role" (CHECKed in the schema)


@dataclass(frozen=True)
class AuditEntry:
    id: int
    actor: str | None
    action: str | None
    target: str | None
    payload_json: str | None
    created_at: str

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> AuditEntry:
        return cls(
            id=row["id"],
            actor=row["actor"],
            action=row["action"],
            target=row["target"],
            payload_json=row["payload_json"],
            created_at=row["created_at"],
        )


def record_audit(
    conn: sqlite3.Connection,
    *,
    actor: Actor,
    action: str,
    target: str | None = None,
    payload: dict | None = None,
) -> int:
    """Append one audit row; returns its id.

    ``payload`` is JSON-encoded. Keep it free of secrets and raw user content —
    this table is for *what happened* (who/what/when), not message bodies (§12).
    """
    payload_json = json.dumps(payload, ensure_ascii=False) if payload is not None else None
    with conn:
        cur = conn.execute(
            "INSERT INTO audit_log (actor, action, target, payload_json) VALUES (?, ?, ?, ?)",
            (actor, action, target, payload_json),
        )
    return int(cur.lastrowid)


def list_audit(
    conn: sqlite3.Connection, *, action: str | None = None, limit: int = 100
) -> list[AuditEntry]:
    """List recent audit entries (newest first), optionally filtered by action."""
    if action is None:
        rows = conn.execute("SELECT * FROM audit_log ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM audit_log WHERE action = ? ORDER BY id DESC LIMIT ?",
            (action, limit),
        ).fetchall()
    return [AuditEntry.from_row(r) for r in rows]
