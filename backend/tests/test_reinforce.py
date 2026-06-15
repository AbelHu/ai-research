"""Tests for reinforcement on use/read (implementation-plan T5.5)."""

from __future__ import annotations

from datetime import datetime

import pytest

from app.config.policies import MemoryPolicy
from app.memory.reinforce import reinforce_memory
from app.storage.db import connect
from app.storage.migrations import migrate
from app.storage.repos import memories as memories_repo

POLICY = MemoryPolicy()


@pytest.fixture
def conn():
    c = connect()
    migrate(c)
    try:
        yield c
    finally:
        c.close()


def test_read_extends_expiry_past_prior(conn) -> None:
    now = datetime(2026, 6, 1, 12, 0, 0)
    prior = "2026-06-03 12:00:00"
    mem = memories_repo.create_memory(
        conn, content="home is Paris", retention_class="long", importance=0.6, expires_at=prior
    )

    refreshed = reinforce_memory(conn, mem.id, now=now, policy=POLICY)

    assert refreshed.use_count == 1
    assert refreshed.last_used_at == "2026-06-01 12:00:00"
    assert datetime.fromisoformat(refreshed.expires_at) > datetime.fromisoformat(prior)


def test_read_nudges_importance_bounded(conn) -> None:
    mem = memories_repo.create_memory(conn, content="x", retention_class="long", importance=0.99)
    refreshed = reinforce_memory(conn, mem.id, policy=POLICY)
    assert refreshed.importance == pytest.approx(1.0)  # capped, not 1.01


def test_lapsed_expiry_anchors_at_now(conn) -> None:
    now = datetime(2026, 6, 1)
    mem = memories_repo.create_memory(
        conn, content="x", retention_class="short", importance=0.5, expires_at="2000-01-01 00:00:00"
    )
    refreshed = reinforce_memory(conn, mem.id, now=now, policy=POLICY)
    assert datetime.fromisoformat(refreshed.expires_at) > now


def test_revive_archived_to_active(conn) -> None:
    mem = memories_repo.create_memory(
        conn, content="archived finding", retention_class="long", importance=0.7
    )
    memories_repo.update_state(conn, mem.id, "archived")

    refreshed = reinforce_memory(conn, mem.id, revive=True)
    assert refreshed.state == "active"  # cold → hot
    assert refreshed.use_count == 1


def test_no_revive_keeps_archived(conn) -> None:
    mem = memories_repo.create_memory(conn, content="x", retention_class="long", importance=0.5)
    memories_repo.update_state(conn, mem.id, "archived")
    refreshed = reinforce_memory(conn, mem.id, revive=False)
    assert refreshed.state == "archived"  # a plain read does not revive


def test_core_memory_expiry_stays_none(conn) -> None:
    mem = memories_repo.create_memory(conn, content="identity", retention_class="core")
    refreshed = reinforce_memory(conn, mem.id, policy=POLICY)
    assert refreshed.expires_at is None
    assert refreshed.use_count == 1


def test_unknown_id_returns_none(conn) -> None:
    assert reinforce_memory(conn, 9999) is None
