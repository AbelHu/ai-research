"""Inspect & migrate the local database (design-spec §9; implementation-plan T1.11).

Run from the ``backend/`` directory:

    python -m app.cli.db --migrate     # apply pending migrations
    python -m app.cli.db --schema      # list tables (and row counts)
    python -m app.cli.db --migrate --schema
    python -m app.cli.db --db /tmp/x.db --schema   # use a specific file

No network, no AI - pure local storage.
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

from app.config.settings import get_settings
from app.storage.db import connect
from app.storage.migrations import applied_versions, migrate

# Default database file lives under the configured data directory.
DEFAULT_DB_NAME = "app.db"


def default_db_path() -> Path:
    return get_settings().data_dir / DEFAULT_DB_NAME


def run_migrate(conn: sqlite3.Connection) -> list[str]:
    """Apply pending migrations; return the versions applied by this call."""
    return migrate(conn)


def list_tables(conn: sqlite3.Connection) -> list[str]:
    """Return user table names (excluding SQLite internal tables), sorted."""
    rows = conn.execute(
        "SELECT name FROM sqlite_master "
        "WHERE type = 'table' AND name NOT LIKE 'sqlite_%' "
        "ORDER BY name"
    ).fetchall()
    return [r[0] for r in rows]


def _print_migrate(conn: sqlite3.Connection) -> None:
    applied = run_migrate(conn)
    if applied:
        print(f"[ok]   Applied {len(applied)} migration(s):")
        for version in applied:
            print(f"         + {version}")
    else:
        print("[ok]   Schema already up to date.")


def _print_schema(conn: sqlite3.Connection) -> None:
    tables = list_tables(conn)
    if not tables:
        print("[info] No tables yet. Run with --migrate first.")
        return
    print(f"Tables ({len(tables)}):")
    for name in tables:
        count = conn.execute(f"SELECT COUNT(*) FROM {name}").fetchone()[0]  # noqa: S608
        print(f"  - {name:<22} {count:>6} rows")
    versions = sorted(applied_versions(conn))
    print(f"\nMigrations applied: {', '.join(versions) if versions else '(none)'}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m app.cli.db",
        description="Migrate and inspect the local SQLite database.",
    )
    parser.add_argument("--migrate", action="store_true", help="apply pending migrations")
    parser.add_argument("--schema", action="store_true", help="list tables + row counts")
    parser.add_argument(
        "--db", type=Path, default=None, help="database file (default: data/app.db)"
    )
    args = parser.parse_args(argv)

    if not args.migrate and not args.schema:
        parser.print_help()
        return 2

    db_path = args.db or default_db_path()
    print(f"Database: {db_path}")
    conn = connect(db_path)
    try:
        if args.migrate:
            _print_migrate(conn)
        if args.schema:
            _print_schema(conn)
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
