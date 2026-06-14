"""Tests for the forward-only migration runner (implementation-plan T1.2)."""

from __future__ import annotations

from app.storage.db import connect
from app.storage.migrations import (
    applied_versions,
    discover_migrations,
    migrate,
)


def test_migrate_is_idempotent() -> None:
    conn = connect()
    try:
        first = migrate(conn)
        # First run applies every discovered migration.
        assert first == [p.stem for p in discover_migrations()]
        # Second run applies nothing new.
        assert migrate(conn) == []
    finally:
        conn.close()


def test_schema_migrations_records_every_version() -> None:
    conn = connect()
    try:
        migrate(conn)
        recorded = applied_versions(conn)
        expected = {p.stem for p in discover_migrations()}
        assert recorded == expected
    finally:
        conn.close()


def test_tracking_table_exists_after_migrate() -> None:
    conn = connect()
    try:
        migrate(conn)
        row = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='schema_migrations'"
        ).fetchone()
        assert row is not None
    finally:
        conn.close()
