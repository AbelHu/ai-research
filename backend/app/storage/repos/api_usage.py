"""Daily API-usage counters — budget caps for metered external services.

A tiny per-(provider, UTC-day) counter used to **cap** how often a metered
external API is called, so a limited quota (e.g. Tavily web-search free credits)
can never be abused. The ``web.search`` skill reads ``count_today`` before each
real call and ``increment``s after a successful one.

Day is the **UTC** date so the cap resets predictably regardless of server
timezone. Writes wrap in ``with conn`` per the repo convention.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone


def _today() -> str:
    return datetime.now(tz=timezone.utc).date().isoformat()


def count_today(conn: sqlite3.Connection, provider: str, *, day: str | None = None) -> int:
    """Return how many calls ``provider`` has made today (0 if none)."""
    row = conn.execute(
        "SELECT count FROM api_usage WHERE provider = ? AND day = ?",
        (provider, day or _today()),
    ).fetchone()
    return int(row["count"]) if row else 0


def increment(
    conn: sqlite3.Connection, provider: str, *, day: str | None = None, amount: int = 1
) -> int:
    """Add ``amount`` to today's count for ``provider``; return the new total.

    Idempotent-by-key upsert: the first call inserts the day row, later calls
    bump it. Safe under the gateway/worker sharing one WAL database.
    """
    d = day or _today()
    with conn:
        conn.execute(
            "INSERT INTO api_usage (provider, day, count, updated_at) "
            "VALUES (?, ?, ?, datetime('now')) "
            "ON CONFLICT(provider, day) DO UPDATE SET "
            "count = count + excluded.count, updated_at = datetime('now')",
            (provider, d, amount),
        )
    return count_today(conn, provider, day=d)
