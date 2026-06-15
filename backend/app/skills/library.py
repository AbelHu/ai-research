"""Library skills — reading from the cold/archived store (design-spec §8.10).

``library.read`` opens an archived item, **revives** it to hot and **reinforces**
its TTL + weight (the cold→hot read path, §9.1).

Until the on-disk folder library + cold zips land in P5 (T5.7/T5.9), the only
cold store is the set of **archived memories**, so ``library.read`` operates on
those by id. It shares the single `touch_memory` reinforcement primitive with
``memory.get`` so P5 has one place to extend.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from app.skills.context import SkillContext
from app.skills.memory import MemoryHit, _to_hit
from app.skills.registry import skill
from app.storage.repos import memories as memories_repo


class ReadLibraryParams(BaseModel):
    memory_id: int = Field(..., ge=1)


class ReadLibraryResult(BaseModel):
    item: MemoryHit | None = None


@skill(
    name="library.read",
    description="Open an archived item; revives it to hot and refreshes TTL + weight.",
    params=ReadLibraryParams,
    returns=ReadLibraryResult,
    permissions=["library.read"],
    effect="read",
)
def library_read(params: ReadLibraryParams, ctx: SkillContext) -> ReadLibraryResult:
    # Cold read: revive archived -> active and reinforce in one touch (§9.1).
    mem = memories_repo.touch_memory(ctx.conn, params.memory_id, revive=True)
    if mem is None:
        return ReadLibraryResult(item=None)
    return ReadLibraryResult(item=_to_hit(ctx, mem))
