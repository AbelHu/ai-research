"""Daily memory sweep (design-spec §9.1; implementation-plan T5.6).

A deterministic 24h maintenance pass over the **active** memory set. Five
ordered, pure phases (the AI only ever *suggests* summaries, validated
elsewhere — the sweep itself runs no model):

  1. **Expire** — items past `expires_at`: **drop** when low-importance &
     unreferenced (delete hot-index rows, keep a thin tombstone), else
     **archive** (recoverable).
  2. **Archive** — `long` items below the effective-weight threshold → archived.
  3. **Promote** — `short` items used enough → `long`.
  4. **Consolidate** — exact-duplicate active items collapse to the oldest; the
     rest are archived and chained via `superseded_by`.
  5. ``core`` is never expired, archived, promoted, or consolidated.

Every transition is reported in `SweepResult` so a caller (and the tests) can
see exactly what changed.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import datetime

from app.config.policies import MemoryPolicy, get_policies
from app.memory.weight import effective_weight
from app.storage.repos import memories as memories_repo
from app.storage.repos.memories import Memory


@dataclass
class SweepResult:
    """Per-phase record of the ids transitioned by a sweep."""

    dropped: list[int] = field(default_factory=list)
    archived: list[int] = field(default_factory=list)
    promoted: list[int] = field(default_factory=list)
    consolidated: list[int] = field(default_factory=list)

    @property
    def total_changed(self) -> int:
        return len(self.dropped) + len(self.archived) + len(self.promoted) + len(self.consolidated)


def _is_expired(mem: Memory, now: datetime) -> bool:
    if mem.expires_at is None:
        return False
    return now >= datetime.fromisoformat(mem.expires_at)


def sweep(
    conn: sqlite3.Connection,
    *,
    now: datetime | None = None,
    policy: MemoryPolicy | None = None,
) -> SweepResult:
    """Run the daily sweep; return the transitions made (§9.1)."""
    moment = now if now is not None else datetime.now()
    pol = policy if policy is not None else get_policies().memory
    result = SweepResult()

    # 1) Expire — drop (low + unreferenced) or archive (important / referenced).
    for mem in memories_repo.list_active(conn):
        if mem.retention_class == "core" or not _is_expired(mem, moment):
            continue
        importance = mem.importance if mem.importance is not None else 0.5
        if importance <= pol.drop_importance_max and mem.use_count == 0:
            memories_repo.drop_memory(conn, mem.id)
            result.dropped.append(mem.id)
        else:
            memories_repo.archive_memory(conn, mem.id)
            result.archived.append(mem.id)

    # 2) Archive — stale `long` items whose effective weight fell below threshold.
    for mem in memories_repo.list_active(conn):
        if mem.retention_class != "long":
            continue
        if effective_weight(mem, now=moment, policy=pol) < pol.archive_threshold:
            memories_repo.archive_memory(conn, mem.id)
            result.archived.append(mem.id)

    # 3) Promote — well-used `short` items graduate to `long`.
    for mem in memories_repo.list_active(conn):
        if mem.retention_class == "short" and mem.use_count >= pol.promote_use_count:
            memories_repo.set_retention_class(conn, mem.id, "long")
            result.promoted.append(mem.id)

    # 4) Consolidate — collapse exact-duplicate content to the oldest row.
    _consolidate(conn, result)

    return result


def _consolidate(conn: sqlite3.Connection, result: SweepResult) -> None:
    """Archive exact-duplicate active memories, chaining them to the keeper."""
    seen: dict[str, int] = {}  # normalized content -> keeper id (lowest)
    for mem in memories_repo.list_active(conn):
        if mem.retention_class == "core" or not mem.content:
            continue
        key = mem.content.strip().lower()
        keeper = seen.get(key)
        if keeper is None:
            seen[key] = mem.id
            continue
        # Duplicate of an earlier (lower-id) memory → archive + chain.
        memories_repo.mark_superseded(conn, mem.id, keeper)
        memories_repo.archive_memory(conn, mem.id)
        result.consolidated.append(mem.id)
