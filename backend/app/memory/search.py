"""FTS5 keyword search over memories (design-spec §9; implementation-plan T5.1).

`keyword_search` runs an FTS5 ``MATCH`` against the `memories_fts` mirror and
returns the matching **active** memories ranked by FTS relevance (best first).
Archived/dropped items are excluded — archived rows are filtered by state, and
dropped rows have already left the index (their content was nulled, §9.1).

User input is turned into a safe OR-of-quoted-terms query so FTS operators in
the raw text can't break the match or inject syntax.
"""

from __future__ import annotations

import sqlite3

from app.storage.repos.memories import Memory


def _safe_match_query(query: str) -> str | None:
    """Build a safe FTS5 MATCH string: ``"t1" OR "t2" ...`` (None if no terms).

    Each whitespace token is wrapped in double quotes (a phrase), with internal
    quotes doubled, so FTS5 special characters in user text are treated as
    literals rather than operators.
    """
    tokens = query.split()
    if not tokens:
        return None
    quoted = [f'"{tok.replace(chr(34), chr(34) * 2)}"' for tok in tokens]
    return " OR ".join(quoted)


def keyword_search(
    conn: sqlite3.Connection,
    query: str,
    *,
    limit: int = 20,
) -> list[Memory]:
    """Return active memories matching ``query`` by FTS5 relevance (best first)."""
    match = _safe_match_query(query)
    if match is None:
        return []
    rows = conn.execute(
        "SELECT m.* FROM memories_fts f "
        "JOIN memories m ON m.id = f.rowid "
        "WHERE memories_fts MATCH ? AND m.state = 'active' "
        "ORDER BY f.rank "
        "LIMIT ?",
        (match, limit),
    ).fetchall()
    return [Memory.from_row(r) for r in rows]


def keyword_search_ranked(
    conn: sqlite3.Connection,
    query: str,
    *,
    limit: int = 20,
) -> list[tuple[int, float]]:
    """Like `keyword_search` but return ``(memory_id, rank)`` pairs (for fusion).

    ``rank`` is FTS5's bm25 score (lower = more relevant); the ordering is
    best-first. Used by the hybrid ranker (T5.3) which only needs ids + order.
    """
    match = _safe_match_query(query)
    if match is None:
        return []
    rows = conn.execute(
        "SELECT f.rowid, f.rank FROM memories_fts f "
        "JOIN memories m ON m.id = f.rowid "
        "WHERE memories_fts MATCH ? AND m.state = 'active' "
        "ORDER BY f.rank "
        "LIMIT ?",
        (match, limit),
    ).fetchall()
    return [(int(r[0]), float(r[1])) for r in rows]
