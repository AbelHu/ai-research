"""Tests for hybrid RRF ranking (implementation-plan T5.3)."""

from __future__ import annotations

import pytest

from app.memory.hybrid import hybrid_search, reciprocal_rank_fusion
from app.memory.vectors import embed_and_store
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


def test_rrf_beats_either_alone() -> None:
    # The core RRF property (plan T5.3): an item that is #1 in *neither* list
    # but present in *both* beats items that top a single list but are absent
    # from the other. fts=[1,2] (1 wins keyword), vec=[3,2] (3 wins vector);
    # item 2 is second in both yet wins the fusion.
    fused = reciprocal_rank_fusion([[1, 2], [3, 2]])
    assert fused[0][0] == 2
    # Confirm 2 topped neither input list.
    assert [lst[0] for lst in ([1, 2], [3, 2])] == [1, 3]


def test_rrf_empty_lists() -> None:
    assert reciprocal_rank_fusion([[], []]) == []


def test_hybrid_orders_by_consensus(conn) -> None:
    # A keyword-stuffed doc (a non-vocabulary term repeated) tops pure FTS tf but
    # carries no semantic signal; the consensus doc matching all query terms +
    # vocabulary should lead the fused ranking, and the stuffed doc trails.
    stuffed = memories_repo.create_memory(conn, content="budget budget budget budget")
    consensus = memories_repo.create_memory(conn, content="budget about Paris France capital")
    semantic = memories_repo.create_memory(conn, content="Paris France capital city")
    for mem in (stuffed, consensus, semantic):
        embed_and_store(conn, mem.id, mem.content, fake_embed)

    query = "budget Paris France capital"
    qvec = fake_embed([query])[0]
    order = [m.id for m in hybrid_search(conn, query, qvec)]

    assert order[0] == consensus.id  # strong in both signals → top
    assert order[-1] == stuffed.id  # keyword-only stuffing → demoted last


def test_hybrid_degrades_to_fts_without_vector(conn) -> None:
    mem = memories_repo.create_memory(conn, content="unique keyword zebra")
    memories_repo.create_memory(conn, content="something else entirely")
    hits = hybrid_search(conn, "zebra", None)
    assert [m.id for m in hits] == [mem.id]


def test_hybrid_excludes_archived(conn) -> None:
    active = memories_repo.create_memory(conn, content="Paris note active")
    archived = memories_repo.create_memory(conn, content="Paris note archived")
    for mem in (active, archived):
        embed_and_store(conn, mem.id, mem.content, fake_embed)
    memories_repo.update_state(conn, archived.id, "archived")

    ids = {m.id for m in hybrid_search(conn, "Paris note", fake_embed(["Paris note"])[0])}
    assert ids == {active.id}
