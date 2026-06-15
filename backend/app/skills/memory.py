"""Memory skills — the typed APIs over local memory (design-spec §8.2, §8.10).

* ``memory.search`` (read)  — hybrid recall; returns candidates, **no** reinforcement.
* ``memory.get``    (read†) — read one memory by id; **reinforces** its TTL/weight.
* ``memory.write``  (local_write) — store a distilled memory (+ optional tags).
* ``memory.tag``    (local_write) — add a normalized tag to a memory.

``†`` ``memory.get`` is read-only on content but performs a reinforcement
bookkeeping write (`last_used_at`/`use_count`/`expires_at`); it never needs
user confirmation (§8.10).
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from app.skills.context import SkillContext
from app.skills.registry import skill
from app.storage.repos import memories as memories_repo
from app.storage.repos.memories import Memory


class MemoryHit(BaseModel):
    """A memory rendered for a skill result (search hit / read view)."""

    id: int
    kind: str | None = None
    content: str | None = None
    summary: str | None = None
    importance: float | None = None
    state: str
    use_count: int
    last_used_at: str | None = None
    expires_at: str | None = None
    tags: list[str] = Field(default_factory=list)


def _to_hit(ctx: SkillContext, mem: Memory) -> MemoryHit:
    return MemoryHit(
        id=mem.id,
        kind=mem.kind,
        content=mem.content,
        summary=mem.summary,
        importance=mem.importance,
        state=mem.state,
        use_count=mem.use_count,
        last_used_at=mem.last_used_at,
        expires_at=mem.expires_at,
        tags=memories_repo.get_tags(ctx.conn, mem.id),
    )


# --- memory.search (T2.5) ---------------------------------------------------


class SearchMemoryParams(BaseModel):
    query: str = Field(..., min_length=1, description="Natural-language search string.")
    limit: int = Field(10, ge=1, le=50)


class SearchMemoryResult(BaseModel):
    hits: list[MemoryHit]


@skill(
    name="memory.search",
    description="Search local memory for relevant items (returns candidates).",
    params=SearchMemoryParams,
    returns=SearchMemoryResult,
    permissions=["memory.read"],
    effect="read",
)
def memory_search(params: SearchMemoryParams, ctx: SkillContext) -> SearchMemoryResult:
    rows = memories_repo.search_memories(ctx.conn, params.query, limit=params.limit)
    return SearchMemoryResult(hits=[_to_hit(ctx, m) for m in rows])


# --- memory.get (T2.6) ------------------------------------------------------


class GetMemoryParams(BaseModel):
    memory_id: int = Field(..., ge=1)


class GetMemoryResult(BaseModel):
    memory: MemoryHit | None = None


@skill(
    name="memory.get",
    description="Read a specific memory by id; refreshes its TTL + weight.",
    params=GetMemoryParams,
    returns=GetMemoryResult,
    permissions=["memory.read"],
    effect="read",
)
def memory_get(params: GetMemoryParams, ctx: SkillContext) -> GetMemoryResult:
    # A deliberate read reinforces the item (§9.1); no revive on the hot path.
    mem = memories_repo.touch_memory(ctx.conn, params.memory_id, revive=False)
    if mem is None:
        return GetMemoryResult(memory=None)
    return GetMemoryResult(memory=_to_hit(ctx, mem))


# --- memory.write (T2.7) ----------------------------------------------------


class WriteMemoryParams(BaseModel):
    content: str = Field(..., min_length=1)
    summary: str | None = None
    kind: str | None = None
    entity_key: str | None = None
    importance: float | None = Field(None, ge=0.0, le=1.0)
    tags: list[str] = Field(default_factory=list)


class WriteMemoryResult(BaseModel):
    memory: MemoryHit


@skill(
    name="memory.write",
    description="Store a distilled memory item (+ optional tags/summary).",
    params=WriteMemoryParams,
    returns=WriteMemoryResult,
    permissions=["memory.write"],
    effect="local_write",
)
def memory_write(params: WriteMemoryParams, ctx: SkillContext) -> WriteMemoryResult:
    mem = memories_repo.create_memory(
        ctx.conn,
        content=params.content,
        summary=params.summary,
        user_id=ctx.user_id,
        kind=params.kind,
        entity_key=params.entity_key,
        importance=params.importance,
    )
    for tag in params.tags:
        memories_repo.add_tag(ctx.conn, mem.id, tag)
    return WriteMemoryResult(memory=_to_hit(ctx, mem))


# --- memory.tag (T2.7) ------------------------------------------------------


class TagMemoryParams(BaseModel):
    memory_id: int = Field(..., ge=1)
    tag: str = Field(..., min_length=1)


class TagMemoryResult(BaseModel):
    memory_id: int
    tags: list[str]


@skill(
    name="memory.tag",
    description="Add a normalized tag to a memory.",
    params=TagMemoryParams,
    returns=TagMemoryResult,
    permissions=["memory.write"],
    effect="local_write",
)
def memory_tag(params: TagMemoryParams, ctx: SkillContext) -> TagMemoryResult:
    memories_repo.add_tag(ctx.conn, params.memory_id, params.tag)
    return TagMemoryResult(
        memory_id=params.memory_id,
        tags=memories_repo.get_tags(ctx.conn, params.memory_id),
    )
