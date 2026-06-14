"""Tests for the memories repository (implementation-plan T1.10)."""

from __future__ import annotations

import pytest

from app.storage.db import connect
from app.storage.migrations import migrate
from app.storage.repos import memories as repo


@pytest.fixture
def conn():
    c = connect()
    migrate(c)
    try:
        yield c
    finally:
        c.close()


def test_create_and_get(conn) -> None:
    mem = repo.create_memory(
        conn,
        content="home is Paris",
        entity_key="location:home",
        retention_class="long",
        importance=0.7,
    )
    assert mem.state == "active"
    assert mem.version == 1
    assert repo.get_memory(conn, mem.id) == mem
    assert repo.get_memory(conn, 9999) is None


def test_search_matches_active_only(conn) -> None:
    hit = repo.create_memory(conn, content="the capital of France is Paris")
    repo.create_memory(conn, content="unrelated note about cats")
    archived = repo.create_memory(conn, content="Paris archived note")
    repo.update_state(conn, archived.id, "archived")

    results = repo.search_memories(conn, "Paris")
    ids = {m.id for m in results}
    assert hit.id in ids
    assert archived.id not in ids  # archived is cold, excluded from hot search


def test_update_state_rejects_bad_value(conn) -> None:
    mem = repo.create_memory(conn, content="x")
    with pytest.raises(ValueError):
        repo.update_state(conn, mem.id, "frozen")


def test_drop_keeps_tombstone_and_superseded_chain(conn) -> None:
    old = repo.create_memory(conn, content="home is Paris", entity_key="location:home")
    new = repo.create_memory(conn, content="home is Lyon", entity_key="location:home")
    # old is superseded by new.
    conn.execute("UPDATE memories SET superseded_by = ? WHERE id = ?", (new.id, old.id))
    # Plant a hot-index embedding row for old.
    conn.execute(
        "INSERT INTO embeddings (object_type, object_id, vector) VALUES (?, ?, ?)",
        (repo.MEMORY_OBJECT_TYPE, old.id, b"\x00\x01"),
    )
    conn.commit()

    repo.drop_memory(conn, old.id)

    # Tombstone row remains, content offloaded, state dropped.
    tomb = repo.get_memory(conn, old.id)
    assert tomb is not None
    assert tomb.state == "dropped"
    assert tomb.content is None
    # superseded_by chain still followable.
    assert tomb.superseded_by == new.id
    # Hot-index embedding row deleted.
    emb = conn.execute(
        "SELECT 1 FROM embeddings WHERE object_type = ? AND object_id = ?",
        (repo.MEMORY_OBJECT_TYPE, old.id),
    ).fetchone()
    assert emb is None
    # The newer memory is untouched.
    assert repo.get_memory(conn, new.id).state == "active"


def test_dropped_memory_excluded_from_search(conn) -> None:
    mem = repo.create_memory(conn, content="droppable Paris fact")
    repo.drop_memory(conn, mem.id)
    assert repo.search_memories(conn, "Paris") == []
