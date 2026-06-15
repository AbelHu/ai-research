"""Vector store + nearest-neighbour search (design-spec §9; implementation-plan T5.2).

Vectors live in the existing `embeddings` table (`(object_type, object_id) →
vector BLOB`). Storage packs floats as little-endian float32 via stdlib
``array`` — compact and dependency-free. Search is a deterministic brute-force
cosine over the stored vectors (the active memories only).

> **Decision (open #3):** we use a **pure-Python** vector store rather than
> ``sqlite-vec`` so the suite stays offline + hash-pinned with no new
> dependency. The query surface (`vector_search`) is intentionally small so a
> `sqlite-vec` backend can replace these internals later without touching
> callers.
"""

from __future__ import annotations

import math
import sqlite3
import sys
from array import array
from collections.abc import Callable, Sequence

from app.storage.repos.memories import MEMORY_OBJECT_TYPE

# An embedder turns texts into vectors (the configured `embedder` model-role).
Embedder = Callable[[Sequence[str]], list[list[float]]]


def pack_vector(vector: Sequence[float]) -> bytes:
    """Pack a float vector into a compact little-endian float32 blob."""
    arr = array("f", vector)
    if arr.itemsize != 4:  # pragma: no cover - 'f' is float32 on all supported platforms
        raise RuntimeError("expected 4-byte float for 'f' array")
    # Normalize byte order so a DB file is portable across architectures.
    if sys.byteorder == "big":  # pragma: no cover - CI is little-endian
        arr.byteswap()
    return arr.tobytes()


def unpack_vector(blob: bytes) -> list[float]:
    """Inverse of `pack_vector`."""
    arr = array("f")
    arr.frombytes(blob)
    if sys.byteorder == "big":  # pragma: no cover - CI is little-endian
        arr.byteswap()
    return list(arr)


def cosine_similarity(a: Sequence[float], b: Sequence[float]) -> float:
    """Cosine similarity in [-1, 1]; 0 when either vector has zero magnitude."""
    if len(a) != len(b):
        raise ValueError(f"vector length mismatch: {len(a)} != {len(b)}")
    dot = 0.0
    na = 0.0
    nb = 0.0
    for x, y in zip(a, b, strict=True):
        dot += x * y
        na += x * x
        nb += y * y
    if na == 0.0 or nb == 0.0:
        return 0.0
    return dot / (math.sqrt(na) * math.sqrt(nb))


def store_embedding(
    conn: sqlite3.Connection,
    object_id: int,
    vector: Sequence[float],
    *,
    object_type: str = MEMORY_OBJECT_TYPE,
) -> None:
    """Upsert the vector for one object into the hot vector index."""
    with conn:
        conn.execute(
            "INSERT INTO embeddings (object_type, object_id, vector) VALUES (?, ?, ?) "
            "ON CONFLICT(object_type, object_id) DO UPDATE SET vector = excluded.vector",
            (object_type, object_id, pack_vector(vector)),
        )


def delete_embedding(
    conn: sqlite3.Connection,
    object_id: int,
    *,
    object_type: str = MEMORY_OBJECT_TYPE,
) -> None:
    """Remove an object's vector from the hot index (archive/drop path)."""
    with conn:
        conn.execute(
            "DELETE FROM embeddings WHERE object_type = ? AND object_id = ?",
            (object_type, object_id),
        )


def embed_and_store(
    conn: sqlite3.Connection,
    memory_id: int,
    text: str,
    embedder: Embedder,
) -> list[float]:
    """Embed ``text`` with the injected embedder and store the memory's vector."""
    vector = embedder([text])[0]
    store_embedding(conn, memory_id, vector)
    return vector


def vector_search(
    conn: sqlite3.Connection,
    query_vector: Sequence[float],
    *,
    limit: int = 20,
    object_type: str = MEMORY_OBJECT_TYPE,
) -> list[tuple[int, float]]:
    """Brute-force nearest neighbours: return ``(memory_id, similarity)`` best-first.

    Only **active** memories are considered (the hot set). Ties break by id so
    results are deterministic.
    """
    rows = conn.execute(
        "SELECT e.object_id, e.vector FROM embeddings e "
        "JOIN memories m ON m.id = e.object_id "
        "WHERE e.object_type = ? AND m.state = 'active'",
        (object_type,),
    ).fetchall()

    scored: list[tuple[int, float]] = []
    for object_id, blob in rows:
        score = cosine_similarity(query_vector, unpack_vector(blob))
        scored.append((int(object_id), score))

    scored.sort(key=lambda pair: (-pair[1], pair[0]))
    return scored[:limit]
