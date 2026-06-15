"""The Coder — generate a feature job's reusable skill, gated (§5/§6B; T6.9/T6.10).

A **feature** job's deliverable is new reusable code. The Coder asks the advisor
for a validated `GeneratedSkill`, then **deterministic code** writes it to disk
**inert** (never imported/executed) under ``app/skills/generated/<job-code>/``.
Activation is a separate, explicit step gated on user confirmation
(`confirm_generated_code`, default on) — the model only proposes the code; it
never runs.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from app.advisor.wrapper import Advisor
from app.skills import codegen
from app.storage.repos import requests as requests_repo


@dataclass(frozen=True)
class CoderResult:
    """A feature job's generated skill — written inert, awaiting confirmation."""

    skill_name: str
    filename: str
    path: Path
    rationale: str
    activated: bool  # always False here (confirmation is a separate gated step)


def generate_feature_skill(
    conn,
    advisor: Advisor,
    *,
    job_id: int,
    goal: str,
    root: Path | None = None,
) -> CoderResult:
    """Generate + write (inert) the reusable skill for a feature job (§5/§6B).

    Returns the `CoderResult` describing the inert bundle. ``root`` overrides the
    generated package location (tests pass a tmp dir); production uses the real
    ``app/skills/generated/`` root. The code is **not** imported or executed.
    """
    root = root if root is not None else codegen.GENERATED_ROOT

    job = requests_repo.get_job(conn, job_id)
    if job is None:
        raise ValueError(f"job {job_id} not found")
    request = requests_repo.get_request(conn, job.request_id)
    if request is None:
        raise ValueError(f"job {job_id} has no request")

    generated = advisor.generate_skill(goal=goal, request_id=request.id, job_id=job_id)

    # Deterministic, inert write — the model's code only lands on disk + the
    # manifest; it is never imported or run until `confirm_and_activate` (§6B).
    path = codegen.write_generated_skill(
        root, request.code, generated.module_filename, generated.code
    )
    return CoderResult(
        skill_name=generated.skill_name,
        filename=generated.module_filename,
        path=path,
        rationale=generated.rationale,
        activated=False,
    )
