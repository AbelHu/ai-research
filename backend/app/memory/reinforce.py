"""Reinforcement on use/read (design-spec §9.1; implementation-plan T5.5).

A deliberate **read** (`memory.get`, `library.read`) or a **use** in a validated
answer is proof an item still matters, so it must refresh the item: bump
`use_count`, stamp `last_used_at = now`, slide `expires_at` forward past its
prior schedule (importance-scaled), nudge `importance` up (bounded), and — for
an archived item read back — **revive** it to ``active`` (cold → hot).

This is the policy-aware layer: it computes the new values via `weight` +
`MemoryPolicy` and commits them through the repo's `apply_reinforcement` setter,
applied **immediately** so the extended TTL takes effect at once (§9.1).
"""

from __future__ import annotations

import sqlite3
from datetime import datetime

from app.config.policies import MemoryPolicy, get_policies
from app.memory.weight import reinforced_expiry
from app.storage.repos import memories as memories_repo
from app.storage.repos.memories import Memory

_DEFAULT_IMPORTANCE = 0.5


def reinforce_memory(
    conn: sqlite3.Connection,
    memory_id: int,
    *,
    revive: bool = False,
    now: datetime | None = None,
    policy: MemoryPolicy | None = None,
) -> Memory | None:
    """Reinforce one memory on read/use; return the refreshed row (or ``None``).

    ``revive`` brings an archived item back to ``active`` (the `library.read`
    path). ``now``/``policy`` are injectable for tests; both default to the live
    clock and the configured `policies.yaml` knobs.
    """
    mem = memories_repo.get_memory(conn, memory_id)
    if mem is None:
        return None

    pol = policy if policy is not None else get_policies().memory
    moment = now if now is not None else datetime.now()
    importance = mem.importance if mem.importance is not None else _DEFAULT_IMPORTANCE

    new_expiry = reinforced_expiry(mem.expires_at, moment, mem.retention_class, importance, pol)
    new_importance = min(1.0, importance + pol.reinforce_importance_step)
    last_used_at = moment.isoformat(sep=" ", timespec="seconds")

    return memories_repo.apply_reinforcement(
        conn,
        memory_id,
        last_used_at=last_used_at,
        expires_at=new_expiry,
        importance=new_importance,
        revive=revive,
    )
