"""SQLite connection helpers (design-spec §9).

Opens SQLite with the project's invariants applied on every connection:
  * **foreign keys enforced** (SQLite leaves them off by default),
  * **WAL** journaling for better concurrency + crash recovery, and
  * row access **by column name** (`sqlite3.Row`).

`connect()` with no path (or ``":memory:"``) opens a private in-memory database,
used by the test suite so nothing touches disk.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

# Passed to sqlite3.connect for a private, transient database (tests).
IN_MEMORY = ":memory:"


def connect(path: str | Path | None = None) -> sqlite3.Connection:
    """Open a SQLite connection with project defaults applied.

    Args:
        path: a filesystem path to the database file. ``None`` or ``":memory:"``
            opens an in-memory database. Parent directories are created for a
            file path.

    Returns:
        A connection with `sqlite3.Row` row factory and pragmas applied.
    """
    if path is None or str(path) == IN_MEMORY:
        target = IN_MEMORY
    else:
        file_path = Path(path)
        file_path.parent.mkdir(parents=True, exist_ok=True)
        target = str(file_path)

    conn = sqlite3.connect(target)
    conn.row_factory = sqlite3.Row
    _apply_pragmas(conn)
    return conn


def _apply_pragmas(conn: sqlite3.Connection) -> None:
    """Apply connection-level pragmas. Must run outside any transaction."""
    # Enforce foreign-key constraints (off by default in SQLite).
    conn.execute("PRAGMA foreign_keys = ON")
    # WAL: concurrent readers + durable, recoverable writes. (No-op for memory.)
    conn.execute("PRAGMA journal_mode = WAL")
    # Wait on a locked database instead of raising immediately.
    conn.execute("PRAGMA busy_timeout = 5000")
