"""Tests for the Librarian role's memory maintenance (design-spec §9.1)."""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from app.roles import librarian
from app.storage.db import connect
from app.storage.migrations import migrate
from app.storage.repos import memories as memories_repo

NOW = datetime(2026, 6, 19, 12, 0)


@pytest.fixture
def conn():
    c = connect()
    migrate(c)
    try:
        yield c
    finally:
        c.close()


def test_run_memory_maintenance_drops_expired_low_value(conn) -> None:
    # An expired, low-importance, unused memory is dropped by the sweep.
    past = (NOW - timedelta(days=1)).isoformat()
    expired = memories_repo.create_memory(
        conn, content="ephemeral note", importance=0.2, retention_class="short", expires_at=past
    )
    # A core memory is never touched.
    core = memories_repo.create_memory(
        conn, content="durable fact", importance=0.9, retention_class="core", expires_at=past
    )

    result = librarian.run_memory_maintenance(conn, now=NOW)

    assert expired.id in result.dropped
    assert core.id not in result.dropped
    assert core.id not in result.archived


def test_run_memory_maintenance_noop_when_nothing_to_do(conn) -> None:
    memories_repo.create_memory(conn, content="fresh note", importance=0.8, retention_class="long")
    result = librarian.run_memory_maintenance(conn, now=NOW)
    assert result.total_changed == 0
