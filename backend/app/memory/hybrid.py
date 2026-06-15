"""Hybrid retrieval — RRF merge of FTS + vector (design-spec §9; plan T5.3).

Reciprocal-rank fusion (RRF) merges the keyword (FTS) and semantic (vector)
result lists into one deterministic ranking. Each list contributes
``1 / (k + rank)`` per item (rank is 0-based, best = 0); the fused score is the
sum across lists, so an item that ranks decently in **both** beats one that wins
only a single list. ``k`` (default 60) damps the contribution of low ranks.

All knobs are explicit and there is no model in the path — purely deterministic
(§9 "Hybrid ranking ... all deterministic").
"""

from __future__ import annotations

import sqlite3
from collections.abc import Sequence

from app.memory.search import keyword_search_ranked
from app.memory.vectors import vector_search
from app.storage.repos.memories import Memory
from app.storage.repos.memories import get_memory as _get_memory

# Default RRF damping constant (Cormack et al.); larger = flatter contribution.
DEFAULT_RRF_K = 60


def reciprocal_rank_fusion(
    ranked_lists: Sequence[Sequence[int]],
    *,
    k: int = DEFAULT_RRF_K,
) -> list[tuple[int, float]]:
    """Fuse ranked id-lists into ``(id, score)`` pairs, best-first.

    Each list is an ordering of object ids (best first). Ties in the fused score
    break by id so the result is deterministic.
    """
    scores: dict[int, float] = {}
    for ranked in ranked_lists:
        for rank, object_id in enumerate(ranked):
            scores[object_id] = scores.get(object_id, 0.0) + 1.0 / (k + rank)
    return sorted(scores.items(), key=lambda pair: (-pair[1], pair[0]))


def hybrid_search(
    conn: sqlite3.Connection,
    query: str,
    query_vector: Sequence[float] | None,
    *,
    limit: int = 20,
    k: int = DEFAULT_RRF_K,
) -> list[Memory]:
    """Return active memories ranked by RRF over FTS + vector (best-first).

    ``query_vector`` may be ``None`` (e.g. no embedder available), in which case
    the result is the keyword ranking alone — hybrid degrades gracefully to FTS.
    """
    fts_ids = [mid for mid, _ in keyword_search_ranked(conn, query, limit=limit * 2)]
    lists: list[list[int]] = [fts_ids]
    if query_vector is not None:
        vec_ids = [mid for mid, _ in vector_search(conn, query_vector, limit=limit * 2)]
        lists.append(vec_ids)

    fused = reciprocal_rank_fusion(lists, k=k)
    out: list[Memory] = []
    for object_id, _score in fused[:limit]:
        mem = _get_memory(conn, object_id)
        if mem is not None and mem.state == "active":
            out.append(mem)
    return out
