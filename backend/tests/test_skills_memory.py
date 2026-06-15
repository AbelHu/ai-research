"""Tests for the memory skills via the runtime (implementation-plan T2.5-T2.7)."""

from __future__ import annotations

import pytest

import app.skills  # noqa: F401  -- ensure @skill registration (auto-discovery)
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
    # The owner user must exist: memory.write stamps ctx.user_id (FK to users).
    cur = conn.execute("INSERT INTO users (display_name, is_owner) VALUES ('owner', 1)")
    user_id = int(cur.lastrowid)
    conn.commit()
    req = requests_repo.create_request(conn)
    job = requests_repo.create_job(conn, request_id=req.id, kind="ask")
    c = SkillContext(
        user_id=user_id,
        conn=conn,
        permissions=frozenset({"memory.read", "memory.write"}),
        job_id=job.id,
    )
    try:
        yield c
    finally:
        conn.close()


# --- T2.5 memory.search -----------------------------------------------------


def test_memory_search_returns_hits(ctx) -> None:
    memories_repo.create_memory(ctx.conn, content="the capital of France is Paris")
    memories_repo.create_memory(ctx.conn, content="unrelated note about cats")

    result = execute("memory.search", {"query": "Paris"}, ctx)

    assert result.ok
    contents = [h.content for h in result.value.hits]
    assert any("Paris" in (c or "") for c in contents)
    assert all("cats" not in (c or "") for c in contents)


# --- T2.6 memory.get (reinforcement) ---------------------------------------


def test_memory_get_reinforces_on_read(ctx) -> None:
    mem = memories_repo.create_memory(
        ctx.conn,
        content="home is Paris",
        expires_at="2000-01-01 00:00:00",  # already lapsed -> slides to now+window
    )
    assert mem.use_count == 0
    assert mem.last_used_at is None

    result = execute("memory.get", {"memory_id": mem.id}, ctx)
    assert result.ok
    assert result.value.memory.id == mem.id

    after = memories_repo.get_memory(ctx.conn, mem.id)
    assert after.use_count == 1
    assert after.last_used_at is not None
    assert after.expires_at > mem.expires_at  # advanced past the prior value


def test_memory_get_missing_returns_none(ctx) -> None:
    result = execute("memory.get", {"memory_id": 9999}, ctx)
    assert result.ok
    assert result.value.memory is None


# --- T2.7 memory.write + memory.tag ----------------------------------------


def test_memory_write_creates_memory_with_tags(ctx) -> None:
    result = execute(
        "memory.write",
        {"content": "user prefers reports at 8am", "tags": ["Preference"], "importance": 0.6},
        ctx,
    )
    assert result.ok
    hit = result.value.memory
    stored = memories_repo.get_memory(ctx.conn, hit.id)
    assert stored is not None
    assert stored.content == "user prefers reports at 8am"
    assert stored.user_id == ctx.user_id
    assert hit.tags == ["preference"]  # normalized


def test_memory_tag_normalizes_and_stores(ctx) -> None:
    mem = memories_repo.create_memory(ctx.conn, content="note")
    result = execute("memory.tag", {"memory_id": mem.id, "tag": "  Daily   Report "}, ctx)
    assert result.ok
    assert result.value.tags == ["daily report"]
    assert memories_repo.get_tags(ctx.conn, mem.id) == ["daily report"]
