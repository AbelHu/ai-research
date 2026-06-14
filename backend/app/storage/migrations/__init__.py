"""Forward-only SQL migrations (design-spec §9, implementation-plan T1.2).

Each migration is a numbered ``NNNN_name.sql`` file in this directory, applied
in lexicographic order **exactly once**. Applied versions are recorded in the
``schema_migrations`` table, so `migrate()` is idempotent: re-running it is a
no-op once everything is applied.

Migrations are forward-only by design — there is no down/rollback. To change the
schema, add a new higher-numbered file.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

MIGRATIONS_DIR = Path(__file__).resolve().parent


def discover_migrations() -> list[Path]:
    """Return migration files sorted by their numeric prefix (ascending)."""
    return sorted(MIGRATIONS_DIR.glob("[0-9]*.sql"))


def _ensure_tracking_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS schema_migrations (
            version    TEXT PRIMARY KEY,
            applied_at TEXT NOT NULL DEFAULT (datetime('now'))
        )
        """
    )
    conn.commit()


def applied_versions(conn: sqlite3.Connection) -> set[str]:
    """Versions already recorded as applied (empty if the table is absent)."""
    _ensure_tracking_table(conn)
    rows = conn.execute("SELECT version FROM schema_migrations").fetchall()
    return {row[0] for row in rows}


def migrate(conn: sqlite3.Connection) -> list[str]:
    """Apply every pending migration in order.

    Returns the list of versions applied by *this* call (empty when the schema
    is already current). Each migration runs in its own transaction; a failure
    rolls that migration back and propagates, leaving earlier ones committed.
    """
    already = applied_versions(conn)
    newly_applied: list[str] = []

    for path in discover_migrations():
        version = path.stem  # e.g. "0001_identity"
        if version in already:
            continue
        sql = path.read_text(encoding="utf-8")
        try:
            conn.executescript(sql)
            conn.execute("INSERT INTO schema_migrations (version) VALUES (?)", (version,))
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        newly_applied.append(version)

    return newly_applied
