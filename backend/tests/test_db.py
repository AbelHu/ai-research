"""Tests for the SQLite connection helper (implementation-plan T1.1)."""

from __future__ import annotations

import sqlite3

from app.storage.db import connect


def test_in_memory_enforces_foreign_keys() -> None:
    conn = connect()
    try:
        assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    finally:
        conn.close()


def test_row_factory_is_row() -> None:
    conn = connect()
    try:
        row = conn.execute("SELECT 1 AS one, 2 AS two").fetchone()
        assert isinstance(row, sqlite3.Row)
        assert row["one"] == 1
        assert row["two"] == 2
    finally:
        conn.close()


def test_foreign_keys_are_actually_enforced() -> None:
    conn = connect()
    try:
        conn.executescript(
            """
            CREATE TABLE parent (id INTEGER PRIMARY KEY);
            CREATE TABLE child (
                id INTEGER PRIMARY KEY,
                parent_id INTEGER NOT NULL REFERENCES parent(id)
            );
            """
        )
        # Inserting a child with a non-existent parent must be rejected.
        try:
            conn.execute("INSERT INTO child (id, parent_id) VALUES (1, 999)")
        except sqlite3.IntegrityError:
            pass
        else:
            raise AssertionError("foreign key violation was not raised")
    finally:
        conn.close()


def test_file_database_is_created(tmp_path) -> None:
    db_path = tmp_path / "nested" / "app.db"
    conn = connect(db_path)
    try:
        conn.execute("CREATE TABLE t (id INTEGER PRIMARY KEY)")
        conn.commit()
    finally:
        conn.close()
    assert db_path.exists()
