"""Tests for the pure-Python vector store + search (implementation-plan T5.2)."""

from __future__ import annotations

import pytest

from app.memory.vectors import (
    cosine_similarity,
    embed_and_store,
    pack_vector,
    store_embedding,
    unpack_vector,
    vector_search,
)
from app.storage.db import connect
from app.storage.migrations import migrate
from app.storage.repos import memories as memories_repo
from tests.fakes import fake_embed


@pytest.fixture
def conn():
    c = connect()
    migrate(c)
    try:
        yield c
    finally:
        c.close()


def test_pack_round_trip() -> None:
    vec = [0.0, 1.5, -2.25, 3.0]
    out = unpack_vector(pack_vector(vec))
    assert out == pytest.approx(vec)


def test_cosine_basics() -> None:
    assert cosine_similarity([1, 0], [1, 0]) == pytest.approx(1.0)
    assert cosine_similarity([1, 0], [0, 1]) == pytest.approx(0.0)
    assert cosine_similarity([1, 0], [-1, 0]) == pytest.approx(-1.0)
    assert cosine_similarity([0, 0], [1, 1]) == 0.0  # zero magnitude guard


def test_nearest_neighbour_returns_expected_id(conn) -> None:
    paris = memories_repo.create_memory(conn, content="the capital of France is Paris")
    soup = memories_repo.create_memory(conn, content="a recipe for onion soup")
    weather = memories_repo.create_memory(conn, content="the weather brings rain")
    for mem in (paris, soup, weather):
        embed_and_store(conn, mem.id, mem.content, fake_embed)

    hits = vector_search(conn, fake_embed(["weather and rain today"])[0])
    assert hits[0][0] == weather.id  # nearest by cosine
    assert hits[0][1] > hits[-1][1]  # best-first ordering


def test_search_excludes_non_active(conn) -> None:
    paris = memories_repo.create_memory(conn, content="capital France Paris")
    archived = memories_repo.create_memory(conn, content="capital France Paris archived")
    embed_and_store(conn, paris.id, paris.content, fake_embed)
    embed_and_store(conn, archived.id, archived.content, fake_embed)
    memories_repo.update_state(conn, archived.id, "archived")

    ids = {mid for mid, _ in vector_search(conn, fake_embed(["Paris France capital"])[0])}
    assert ids == {paris.id}


def test_store_is_upsert(conn) -> None:
    mem = memories_repo.create_memory(conn, content="x")
    store_embedding(conn, mem.id, [1.0, 0.0])
    store_embedding(conn, mem.id, [0.0, 1.0])  # overwrite, not duplicate
    count = conn.execute(
        "SELECT COUNT(*) FROM embeddings WHERE object_type = 'memory' AND object_id = ?",
        (mem.id,),
    ).fetchone()[0]
    assert count == 1
