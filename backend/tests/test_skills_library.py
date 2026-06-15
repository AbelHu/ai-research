"""Tests for library.read — cold-read revive + reinforce (T2.6)."""

from __future__ import annotations

import pytest

import app.skills  # noqa: F401  -- ensure @skill registration
from app.skills.context import SkillContext
from app.skills.runtime import execute
from app.storage.db import connect
from app.storage.migrations import migrate
from app.storage.repos import memories as memories_repo
from app.storage.repos import requests as requests_repo


@pytest.fixture
def ctx():
    conn = connect()
    migrate(conn)
    req = requests_repo.create_request(conn)
    job = requests_repo.create_job(conn, request_id=req.id, kind="ask")
    c = SkillContext(
        user_id=1,
        conn=conn,
        permissions=frozenset({"library.read"}),
        job_id=job.id,
    )
    try:
        yield c
    finally:
        conn.close()


def test_library_read_revives_and_reinforces(ctx) -> None:
    mem = memories_repo.create_memory(
        ctx.conn,
        content="archived finding about Paris",
        expires_at="2000-01-01 00:00:00",
    )
    memories_repo.update_state(ctx.conn, mem.id, "archived")

    result = execute("library.read", {"memory_id": mem.id}, ctx)
    assert result.ok
    assert result.value.item.id == mem.id

    after = memories_repo.get_memory(ctx.conn, mem.id)
    assert after.state == "active"  # cold -> hot
    assert after.use_count == 1
    assert after.last_used_at is not None
    assert after.expires_at > mem.expires_at


def test_library_read_missing_returns_none(ctx) -> None:
    result = execute("library.read", {"memory_id": 9999}, ctx)
    assert result.ok
    assert result.value.item is None
