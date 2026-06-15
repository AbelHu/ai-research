"""Tests for the daily memory sweep (implementation-plan T5.6)."""

from __future__ import annotations

from datetime import datetime

import pytest

from app.config.policies import MemoryPolicy
from app.memory.sweep import sweep
from app.storage.db import connect
from app.storage.migrations import migrate
from app.storage.repos import memories as memories_repo

POLICY = MemoryPolicy()
NOW = datetime(2026, 6, 15, 12, 0, 0)
PAST = "2026-01-01 00:00:00"  # well before NOW
FUTURE = "2027-01-01 00:00:00"  # after NOW


@pytest.fixture
def conn():
    c = connect()
    migrate(c)
    try:
        yield c
    finally:
        c.close()


def test_expired_unimportant_unreferenced_is_dropped(conn) -> None:
    mem = memories_repo.create_memory(
        conn, content="one-off lookup", retention_class="ephemeral", importance=0.1, expires_at=PAST
    )
    result = sweep(conn, now=NOW, policy=POLICY)

    assert mem.id in result.dropped
    after = memories_repo.get_memory(conn, mem.id)
    assert after.state == "dropped"
    assert after.content is None  # tombstone


def test_expired_important_is_archived_not_dropped(conn) -> None:
    mem = memories_repo.create_memory(
        conn, content="home is Paris", retention_class="long", importance=0.9, expires_at=PAST
    )
    result = sweep(conn, now=NOW, policy=POLICY)

    assert mem.id in result.archived
    assert mem.id not in result.dropped
    after = memories_repo.get_memory(conn, mem.id)
    assert after.state == "archived"
    assert after.content == "home is Paris"  # never destroyed


def test_expired_referenced_low_importance_is_archived(conn) -> None:
    mem = memories_repo.create_memory(
        conn, content="seen before", retention_class="short", importance=0.2, expires_at=PAST
    )
    # use_count > 0 → referenced → archived, not dropped (the spec's drop rule).
    conn.execute("UPDATE memories SET use_count = 3 WHERE id = ?", (mem.id,))
    conn.commit()

    result = sweep(conn, now=NOW, policy=POLICY)
    assert mem.id in result.archived
    assert memories_repo.get_memory(conn, mem.id).state == "archived"


def test_unexpired_item_survives(conn) -> None:
    mem = memories_repo.create_memory(
        conn, content="still fresh", retention_class="short", importance=0.3, expires_at=FUTURE
    )
    result = sweep(conn, now=NOW, policy=POLICY)
    assert mem.id not in result.dropped + result.archived
    assert memories_repo.get_memory(conn, mem.id).state == "active"


def test_core_is_never_touched(conn) -> None:
    mem = memories_repo.create_memory(
        conn, content="identity", retention_class="core", importance=1.0, expires_at=PAST
    )
    result = sweep(conn, now=NOW, policy=POLICY)
    assert mem.id not in result.dropped + result.archived
    assert memories_repo.get_memory(conn, mem.id).state == "active"


def test_well_used_short_is_promoted_to_long(conn) -> None:
    mem = memories_repo.create_memory(
        conn, content="recurring detail", retention_class="short", importance=0.5, expires_at=FUTURE
    )
    conn.execute(
        "UPDATE memories SET use_count = ? WHERE id = ?", (POLICY.promote_use_count, mem.id)
    )
    conn.commit()

    result = sweep(conn, now=NOW, policy=POLICY)
    assert mem.id in result.promoted
    assert memories_repo.get_memory(conn, mem.id).retention_class == "long"


def test_exact_duplicates_consolidate_to_oldest(conn) -> None:
    keeper = memories_repo.create_memory(
        conn, content="the sky is blue", retention_class="short", importance=0.5, expires_at=FUTURE
    )
    dupe = memories_repo.create_memory(
        conn, content="The Sky Is Blue", retention_class="short", importance=0.5, expires_at=FUTURE
    )
    result = sweep(conn, now=NOW, policy=POLICY)

    assert dupe.id in result.consolidated
    assert keeper.id not in result.consolidated
    after_dupe = memories_repo.get_memory(conn, dupe.id)
    assert after_dupe.state == "archived"
    assert after_dupe.superseded_by == keeper.id
    assert memories_repo.get_memory(conn, keeper.id).state == "active"


def test_sweep_is_idempotent_on_a_clean_set(conn) -> None:
    memories_repo.create_memory(
        conn, content="fresh", retention_class="long", importance=0.9, expires_at=FUTURE
    )
    first = sweep(conn, now=NOW, policy=POLICY)
    second = sweep(conn, now=NOW, policy=POLICY)
    assert first.total_changed == 0
    assert second.total_changed == 0
