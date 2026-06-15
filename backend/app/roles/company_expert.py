"""The Company Expert — plan/phase sign-off (design-spec §6B; implementation-plan T6.3).

Standing company-context reviewer. It calls the advisor for a validated
`Verdict` (approve/decline) and then **deterministic code** applies the status
change and **bounds the decline loop**: a phase may be declined at most
``max_phase_declines`` times (default 3); beyond that the Boss/PM escalate to the
user instead of looping forever (§6B). The model only advises the verdict — it
never drives the control path.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.advisor.schemas import Verdict
from app.advisor.wrapper import Advisor
from app.config.policies import get_policies
from app.roles.envelope import Role
from app.storage.repos import plans as plans_repo
from app.storage.repos.plans import Phase, Plan

_ACTOR = Role.company_expert


@dataclass(frozen=True)
class PlanReview:
    verdict: Verdict
    approved: bool


@dataclass(frozen=True)
class PhaseReview:
    verdict: Verdict
    decision: str  # "approve" | "decline"
    escalate: bool  # decline cap reached → PM escalates to the user


def review_plan(
    conn,
    advisor: Advisor,
    plan: Plan,
    *,
    request_id: int,
    context: str = "",
) -> PlanReview:
    """Approve or decline a drafted plan; on approve, cascade to Approved (§6B)."""
    verdict = advisor.review(
        subject="the proposed plan",
        context=context,
        request_id=request_id,
        job_id=plan.job_id,
    )
    approved = verdict.decision == "approve"
    if approved:
        plans_repo.approve_plan(conn, plan.id, actor=_ACTOR)
    return PlanReview(verdict=verdict, approved=approved)


def review_phase(
    conn,
    advisor: Advisor,
    phase: Phase,
    *,
    request_id: int,
    job_id: int | None = None,
    context: str = "",
    max_phase_declines: int | None = None,
) -> PhaseReview:
    """Sign off (or decline) a resolved phase, bounding the decline loop (§6B).

    Approve → phase + its resolved tasks ``Resolved -> Closed``. Decline →
    if the cap is already reached, **escalate** (leave the phase ``Resolved``);
    otherwise bump the decline count and send the phase ``Resolved -> Active``
    for rework.
    """
    cap = (
        max_phase_declines if max_phase_declines is not None else get_policies().max_phase_declines
    )
    verdict = advisor.review(
        subject=phase.title or "the completed phase",
        context=context,
        request_id=request_id,
        job_id=job_id,
    )

    if verdict.decision == "approve":
        plans_repo.set_phase_status(conn, phase.id, "Closed", actor=_ACTOR)
        for task in plans_repo.list_tasks(conn, phase.id):
            if task.status == "Resolved":
                plans_repo.set_task_status(conn, task.id, "Closed", actor=_ACTOR)
        return PhaseReview(verdict=verdict, decision="approve", escalate=False)

    # Decline: escalate at the cap, else reactivate for rework.
    if phase.decline_count >= cap:
        return PhaseReview(verdict=verdict, decision="decline", escalate=True)
    plans_repo.set_phase_status(conn, phase.id, "Active", actor=_ACTOR, bump_decline=True)
    return PhaseReview(verdict=verdict, decision="decline", escalate=False)
