"""Skill runtime — the only place skills execute (design-spec §8.6, T2.4).

``execute()`` is the deterministic boundary between an AI *suggestion* and an
actual call. Every proposal follows the same pipeline:

    catalog -> proposal -> validate params -> policy gate -> run -> record

An unknown or invalid proposal is rejected **before** any code runs; an
``external`` effect returns ``pending_confirmation`` instead of executing.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone

from pydantic import BaseModel, ValidationError

from app.skills import policy
from app.skills.context import SkillContext
from app.skills.registry import SkillSpec, get_skill
from app.storage.repos import steps as steps_repo


class UnknownSkill(KeyError):
    """Raised when a proposal names a skill that isn't in the registry."""

    def __init__(self, name: str) -> None:
        self.name = name
        super().__init__(name)


class InvalidParams(ValueError):
    """Raised when raw params fail validation against the skill's schema."""

    def __init__(self, name: str, error: ValidationError) -> None:
        self.name = name
        self.error = error
        super().__init__(f"invalid params for skill {name!r}: {error}")


@dataclass(frozen=True)
class SkillResult:
    """Outcome of an `execute()` call."""

    name: str
    status: str  # "ok" | "pending_confirmation"
    value: BaseModel | None = None
    params: BaseModel | None = None
    started_at: str | None = None
    ended_at: str | None = None
    step_id: int | None = None

    @property
    def ok(self) -> bool:
        return self.status == "ok"

    @property
    def pending_confirmation(self) -> bool:
        return self.status == "pending_confirmation"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def execute(name: str, raw_params: dict, ctx: SkillContext) -> SkillResult:
    """Validate, gate, run and record a single skill call (§8.6)."""
    spec = get_skill(name)
    if spec is None:
        raise UnknownSkill(name)  # AI hallucinated a skill -> rejected.

    # 1) Schema validation — the model can't malform a call past this point.
    try:
        params = spec.params_model.model_validate(raw_params)
    except ValidationError as exc:
        raise InvalidParams(name, exc) from exc

    # 2) Policy gate — permissions, then the effect-class confirmation rule.
    policy.check(spec, ctx)
    if policy.needs_confirmation(spec, ctx):
        return SkillResult(name=name, status="pending_confirmation", params=params)

    # 3) Run the pure function.
    started = _now_iso()
    value = spec.fn(params, ctx)
    ended = _now_iso()

    # 4) Record the step (the "process"). Requires a job context; the running
    #    control loop always supplies one. Pure-function tests bypass execute().
    step_id = _record(ctx, spec, params, value, started, ended)
    return SkillResult(
        name=name,
        status="ok",
        value=value,
        params=params,
        started_at=started,
        ended_at=ended,
        step_id=step_id,
    )


def _record(
    ctx: SkillContext,
    spec: SkillSpec,
    params: BaseModel,
    value: BaseModel,
    started: str,
    ended: str,
) -> int | None:
    if ctx.job_id is None:
        return None
    provenance = {"skill": spec.name, "started_at": started, "ended_at": ended}
    return steps_repo.record_step(
        ctx.conn,
        job_id=ctx.job_id,
        plan_task_id=ctx.task_id,
        skill_name=spec.name,
        status="ok",
        params_json=params.model_dump_json(),
        result_json=value.model_dump_json(),
        provenance_json=json.dumps(provenance),
        started_at=started,
        ended_at=ended,
    )
