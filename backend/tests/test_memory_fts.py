"""Tests for FTS5 keyword search over memories (implementation-plan T5.1)."""

from __future__ import annotations

import pytest

from app.memory.search import keyword_search
from app.storage.db import connect
from app.storage.migrations import migrate
from app.storage.repos import memories as memories_repo


@pytest.fixture
def conn():
    c = connect()
    migrate(c)
    try:
        yield c
    finally:
        c.close()


def test_indexes_and_matches_by_keyword(conn) -> None:
    memories_repo.create_memory(conn, content="the capital of France is Paris")
    memories_repo.create_memory(conn, content="a recipe for onion soup")
    memories_repo.create_memory(conn, content="Paris hosts the Olympic games")

    hits = keyword_search(conn, "Paris")
    contents = [m.content for m in hits]
    assert len(hits) == 2
    assert all("Paris" in c for c in contents)


def test_ranks_relevant_first(conn) -> None:
    # Two mentions of "vector" should outrank a single mention.
    one = memories_repo.create_memory(conn, content="a vector is an array")
    many = memories_repo.create_memory(
        conn, content="vector vector vector search ranks vectors", summary="vector notes"
    )

    hits = keyword_search(conn, "vector")
    assert [m.id for m in hits][0] == many.id
    assert one.id in {m.id for m in hits}


def test_excludes_archived_and_dropped(conn) -> None:
    active = memories_repo.create_memory(conn, content="active Paris note")
    archived = memories_repo.create_memory(conn, content="archived Paris note")
    dropped = memories_repo.create_memory(conn, content="dropped Paris note")
    memories_repo.update_state(conn, archived.id, "archived")
    memories_repo.drop_memory(conn, dropped.id)

    ids = {m.id for m in keyword_search(conn, "Paris")}
    assert ids == {active.id}


def test_matches_summary_field(conn) -> None:
    mem = memories_repo.create_memory(
        conn, content="opaque body", summary="quarterly revenue forecast"
    )
    hits = keyword_search(conn, "revenue")
    assert mem.id in {m.id for m in hits}


def test_special_characters_do_not_break_query(conn) -> None:
    memories_repo.create_memory(conn, content="email is a@b.com about budgets")
    # FTS operators / punctuation in the query are treated as literals.
    assert keyword_search(conn, "budgets OR (a@b.com)") != []
    assert keyword_search(conn, '"') == []  # only punctuation -> no terms matched


def test_empty_query_returns_nothing(conn) -> None:
    memories_repo.create_memory(conn, content="something")
    assert keyword_search(conn, "   ") == []
