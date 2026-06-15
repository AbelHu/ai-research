"""Memories repository (design-spec §9, §9.1; implementation-plan T1.10).

Typed create/get/search(stub)/update-state plus the **drop** rule: dropping a
memory deletes its *hot-index* rows (embeddings here; `*_fts` arrive in P5) and
keeps a **thin tombstone** (`state='dropped'`, content/summary offloaded) so FK
references and `superseded_by`/`version` chains stay intact (§9.1).
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

# Addresses a memory's rows in the shared (object_type, object_id) hot indexes.
MEMORY_OBJECT_TYPE = "memory"


@dataclass(frozen=True)
class Memory:
    id: int
    user_id: int | None
    kind: str | None
    entity_key: str | None
    content: str | None
    summary: str | None
    importance: float | None
    retention_class: str | None
    confidence: float | None
    use_count: int
    last_used_at: str | None
    expires_at: str | None
    version: int
    superseded_by: int | None
    state: str
    source_ref: str | None
    created_at: str
    updated_at: str

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> Memory:
        return cls(
            id=row["id"],
            user_id=row["user_id"],
            kind=row["kind"],
            entity_key=row["entity_key"],
            content=row["content"],
            summary=row["summary"],
            importance=row["importance"],
            retention_class=row["retention_class"],
            confidence=row["confidence"],
            use_count=row["use_count"],
            last_used_at=row["last_used_at"],
            expires_at=row["expires_at"],
            version=row["version"],
            superseded_by=row["superseded_by"],
            state=row["state"],
            source_ref=row["source_ref"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )


_VALID_STATES = {"active", "archived", "dropped"}


def create_memory(
    conn: sqlite3.Connection,
    *,
    content: str,
    summary: str | None = None,
    user_id: int | None = None,
    kind: str | None = None,
    entity_key: str | None = None,
    importance: float | None = None,
    retention_class: str | None = None,
    confidence: float | None = None,
    expires_at: str | None = None,
    source_ref: str | None = None,
) -> Memory:
    """Insert an active memory. Returns the stored row."""
    with conn:
        cur = conn.execute(
            "INSERT INTO memories "
            "(user_id, kind, entity_key, content, summary, importance, "
            " retention_class, confidence, expires_at, source_ref) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                user_id,
                kind,
                entity_key,
                content,
                summary,
                importance,
                retention_class,
                confidence,
                expires_at,
                source_ref,
            ),
        )
    got = get_memory(conn, int(cur.lastrowid))
    assert got is not None  # just inserted
    return got


def get_memory(conn: sqlite3.Connection, memory_id: int) -> Memory | None:
    row = conn.execute("SELECT * FROM memories WHERE id = ?", (memory_id,)).fetchone()
    return Memory.from_row(row) if row else None


def search_memories(
    conn: sqlite3.Connection,
    query: str,
    *,
    limit: int = 20,
) -> list[Memory]:
    """Stub recall: case-insensitive substring match over **active** memories.

    The hybrid FTS + vector ranking lands in P5; this keeps the repo usable now
    and never returns archived/dropped (cold) items.
    """
    like = f"%{query}%"
    rows = conn.execute(
        "SELECT * FROM memories "
        "WHERE state = 'active' AND (content LIKE ? OR summary LIKE ?) "
        "ORDER BY id DESC LIMIT ?",
        (like, like, limit),
    ).fetchall()
    return [Memory.from_row(r) for r in rows]


def update_state(conn: sqlite3.Connection, memory_id: int, state: str) -> None:
    """Set a memory's lifecycle state (active | archived | dropped)."""
    if state not in _VALID_STATES:
        raise ValueError(f"invalid memory state: {state!r}")
    with conn:
        conn.execute(
            "UPDATE memories SET state = ?, updated_at = datetime('now') WHERE id = ?",
            (state, memory_id),
        )


def drop_memory(conn: sqlite3.Connection, memory_id: int) -> None:
    """Drop a memory: delete hot-index rows, keep a thin tombstone (§9.1).

    Deletes the memory's `embeddings` row (hot index) and offloads
    `content`/`summary` (set to NULL here; the on-disk dropped store is wired in
    P5), while keeping the `memories` row with ``state='dropped'`` so foreign
    keys and `superseded_by`/`version` chains remain followable.
    """
    with conn:
        conn.execute(
            "DELETE FROM embeddings WHERE object_type = ? AND object_id = ?",
            (MEMORY_OBJECT_TYPE, memory_id),
        )
        conn.execute(
            "UPDATE memories "
            "SET state = 'dropped', content = NULL, summary = NULL, "
            "    updated_at = datetime('now') "
            "WHERE id = ?",
            (memory_id,),
        )


# Reinforcement window applied on read/use (implementation-plan T2.6). This is a
# deliberately simple fixed slide; P5 (T5.4/T5.5) replaces it with the
# importance-scaled, decay-aware formula from §9.1.
REINFORCE_WINDOW = "+7 days"


def touch_memory(
    conn: sqlite3.Connection,
    memory_id: int,
    *,
    revive: bool = False,
) -> Memory | None:
    """Reinforce a memory on read/use (§9.1; implementation-plan T2.6).

    A deliberate read is proof the item is still useful, so it must refresh:
    bump `use_count`, set `last_used_at = now`, and slide `expires_at` forward
    past whatever it was scheduled to be (or `now` if it had already lapsed).
    With ``revive=True`` an ``archived`` item is brought back to ``active`` —
    the cold→hot path used by ``library.read``.

    Returns the refreshed row, or ``None`` if the id is unknown.
    """
    if get_memory(conn, memory_id) is None:
        return None
    with conn:
        conn.execute(
            "UPDATE memories "
            "SET use_count = use_count + 1, "
            "    last_used_at = datetime('now'), "
            "    expires_at = datetime("
            "        CASE WHEN expires_at IS NULL OR expires_at < datetime('now') "
            "             THEN datetime('now') ELSE expires_at END, ?), "
            "    state = CASE WHEN ? AND state = 'archived' THEN 'active' ELSE state END, "
            "    updated_at = datetime('now') "
            "WHERE id = ?",
            (REINFORCE_WINDOW, 1 if revive else 0, memory_id),
        )
    return get_memory(conn, memory_id)


def normalize_tag(tag: str) -> str:
    """Normalize a tag: trim, lowercase, collapse internal whitespace.

    Raises ``ValueError`` for an empty/whitespace-only tag.
    """
    norm = " ".join(tag.strip().lower().split())
    if not norm:
        raise ValueError("tag must be non-empty")
    return norm


def add_tag(conn: sqlite3.Connection, memory_id: int, tag: str) -> str:
    """Attach a normalized tag to a memory (idempotent). Returns the stored tag."""
    norm = normalize_tag(tag)
    with conn:
        conn.execute(
            "INSERT OR IGNORE INTO memory_tags (memory_id, tag) VALUES (?, ?)",
            (memory_id, norm),
        )
    return norm


def get_tags(conn: sqlite3.Connection, memory_id: int) -> list[str]:
    """Return a memory's tags, sorted."""
    rows = conn.execute(
        "SELECT tag FROM memory_tags WHERE memory_id = ? ORDER BY tag",
        (memory_id,),
    ).fetchall()
    return [r[0] for r in rows]
