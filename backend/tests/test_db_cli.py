"""Tests for the db CLI (implementation-plan T1.11)."""

from __future__ import annotations

import pytest

from app.cli import db as cli
from app.storage.db import connect
from app.storage.migrations import discover_migrations


@pytest.fixture
def db_path(tmp_path):
    return tmp_path / "app.db"


def test_migrate_then_schema_lists_tables(db_path, capsys) -> None:
    assert cli.main(["--db", str(db_path), "--migrate"]) == 0
    out = capsys.readouterr().out
    assert "Applied" in out

    assert cli.main(["--db", str(db_path), "--schema"]) == 0
    out = capsys.readouterr().out
    # A few representative tables from across the migrations must appear.
    for table in ("users", "requests", "jobs", "memories", "schema_migrations"):
        assert table in out


def test_migrate_is_idempotent_via_cli(db_path, capsys) -> None:
    cli.main(["--db", str(db_path), "--migrate"])
    capsys.readouterr()
    # Second migrate reports nothing new.
    assert cli.main(["--db", str(db_path), "--migrate"]) == 0
    assert "already up to date" in capsys.readouterr().out


def test_no_flags_prints_help_and_returns_2(capsys) -> None:
    assert cli.main([]) == 2
    assert "usage" in capsys.readouterr().out.lower()


def test_list_tables_matches_migrations(db_path) -> None:
    conn = connect(db_path)
    try:
        cli.run_migrate(conn)
        tables = cli.list_tables(conn)
        # schema_migrations + at least one table per migration file.
        assert "schema_migrations" in tables
        assert len(tables) > len(discover_migrations())
    finally:
        conn.close()
