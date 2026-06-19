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
from app.storage.repos import requests as requests_repo
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


def _goal_for(conn, request_id: int) -> str:
    """The original request goal (the user's own title), id-free for the prompt."""
    request = requests_repo.get_request(conn, request_id)
    return (request.title if request else "") or "(goal not recorded)"


def _render_plan_for_review(conn, plan: Plan) -> str:
    """A reviewable, id-free rendering of a plan: phases, tasks, success criteria.

    The reviewer can only judge a plan it can actually see; we serialize the
    plan's **content** (titles + criteria) — never internal ids/codes (§ prompt
    privacy) — so the verdict is grounded in the real approach, not an empty stub.
    """
    lines: list[str] = ["Proposed plan:"]
    for phase in plans_repo.list_phases(conn, plan.id):
        lines.append(f"- Phase: {phase.title or '(untitled phase)'}")
        for task in plans_repo.list_tasks(conn, phase.id):
            lines.append(f"    - {task.title or '(untitled task)'}")
    stored = plans_repo.get_plan(conn, plan.id)
    criteria = stored.success_criteria if stored else []
    if criteria:
        lines.append("")
        lines.append("Success criteria the work must meet:")
        lines.extend(f"- {c}" for c in criteria)
    return "\n".join(lines)


def _render_phase_for_review(conn, phase: Phase) -> str:
    """A reviewable, id-free rendering of a completed phase: its tasks + statuses."""
    lines = [f"Completed phase: {phase.title or '(untitled phase)'}", "Tasks:"]
    tasks = plans_repo.list_tasks(conn, phase.id)
    if tasks:
        lines.extend(f"- {t.title or '(untitled task)'} [{t.status}]" for t in tasks)
    else:
        lines.append("- (no tasks recorded)")
    return "\n".join(lines)


def review_plan(
    conn,
    advisor: Advisor,
    plan: Plan,
    *,
    request_id: int,
    context: str = "",
) -> PlanReview:
    """Approve or decline a drafted plan; on approve, cascade to Approved (§6B)."""
    goal_context = f"Original goal: {_goal_for(conn, request_id)}"
    full_context = f"{goal_context}\n\n{context}".strip() if context else goal_context
    verdict = advisor.review(
        subject=_render_plan_for_review(conn, plan),
        context=full_context,
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
    goal_context = f"Original goal: {_goal_for(conn, request_id)}"
    full_context = f"{goal_context}\n\n{context}".strip() if context else goal_context
    verdict = advisor.review(
        subject=_render_phase_for_review(conn, phase),
        context=full_context,
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
