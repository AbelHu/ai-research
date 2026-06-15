"""Per-job execution orchestration — run a planned job end-to-end (§6B).

The keystone that turns a **planned** job (a complex ``task``/``feature``, or a
simple ask escalated by the control loop) into a delivered result by driving the
already-built execution roles in the design's order:

    Analyzer.draft_plan
      → Company Expert.review_plan (approve)
        → for each phase:  Boss(Active) → Senior.run_phase
                           → Plan Expert.resolve_phase → Company Expert.review_phase
          → Plan Expert.assemble_final_report
            → PM delivery

Deterministic code drives every status transition (§6B who-sets-what); the model
only advises (plan content, sign-off verdict, each task's next action). This is
the **synchronous** core — the async per-job runner (scheduler + pause/abandon,
T6.4/T6.7) wraps a call to :func:`execute_planned_job` as its phase work, and the
service delivers the returned message back to the user as a progress update.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.advisor.wrapper import Advisor
from app.memory.reports import FinalReport
from app.roles import analyzer, company_expert, plan_expert, pm, senior
from app.roles.envelope import Role
from app.storage.repos import plans as plans_repo
from app.storage.repos import requests as requests_repo
from app.storage.repos.requests import Request

# Terminal status of a job execution (what the PM would surface to the user).
JobStatus = str  # "completed" | "plan_declined" | "phase_escalated"


@dataclass(frozen=True)
class JobOutcome:
    """The result of executing one planned job (mirrors `AskOutcome` for asks)."""

    status: JobStatus
    job_id: int
    request: Request
    plan_id: int | None = None
    report: FinalReport | None = None
    delivery: str | None = None  # PM-formatted user message


def card_for_job(conn, job_id: int) -> dict:
    """Rebuild the §6D `RequestCard` for a job from the DB (background runner).

    The control loop has the card in hand, but a background runner picks a job up
    by id, so it reconstructs the card from the linked request. Internal ids stay
    out of the model prompt (the Analyzer/advisor only see ``text``/``title``).
    """
    job = requests_repo.get_job(conn, job_id)
    if job is None:
        raise ValueError(f"job {job_id} not found")
    request = requests_repo.get_request(conn, job.request_id)
    if request is None:
        raise ValueError(f"job {job_id} has no request")
    return {
        "request_id": request.id,
        "request_code": request.code,
        "title": request.title,
        "text": request.title or "",
        "append": False,
    }


def _run_one_phase(
    conn,
    advisor: Advisor,
    phase,
    *,
    request_id: int,
    job_id: int,
    user_id: int | None,
) -> str:
    """Drive one phase Approved → Closed; return the sign-off decision.

    Boss activates the phase, the Senior Worker runs its tasks, the Plan Expert
    resolves it, and the Company Expert signs it off — each step a §6B-legal
    transition. Returns ``"approve"`` (phase Closed) or ``"decline"``.
    """
    # Boss: phase Approved → Active.
    plans_repo.set_phase_status(conn, phase.id, "Active", actor=Role.boss)

    # Senior Worker: run the phase's tasks (Active → InProgress, tasks → Resolved).
    senior.run_phase(conn, advisor, phase, request_id=request_id, job_id=job_id, user_id=user_id)

    # Plan Expert: resolve the phase once its tasks are done (InProgress → Resolved).
    plan_expert.resolve_phase(conn, plans_repo.get_phase(conn, phase.id))

    # Company Expert: sign off (Resolved → Closed) or decline.
    review = company_expert.review_phase(
        conn,
        advisor,
        plans_repo.get_phase(conn, phase.id),
        request_id=request_id,
        job_id=job_id,
    )
    return review.decision


def execute_planned_job(
    conn,
    advisor: Advisor,
    *,
    job_id: int,
    card: dict | None = None,
    user_id: int | None = None,
) -> JobOutcome:
    """Run a planned job to a delivered result (§6B). Synchronous + deterministic.

    ``card`` is the §6D request card; when omitted it's reconstructed from the
    job (the background runner path). Returns a `JobOutcome` whose ``delivery`` is
    the PM-formatted message to send the user. Stops + reports honestly if the
    plan is declined or a phase escalates (the decline cap is the Company
    Expert's, §6B) rather than looping unbounded.
    """
    if card is None:
        card = card_for_job(conn, job_id)
    request_id = card["request_id"]
    request = requests_repo.get_request(conn, request_id)
    if request is None:
        raise ValueError(f"request {request_id} not found")

    # 1) Analyzer drafts + persists the plan (phases → tasks, all New).
    plan = analyzer.draft_plan(conn, advisor, card, job_id=job_id)

    # 2) Company Expert reviews the plan; approval cascades New → Approved.
    review = company_expert.review_plan(conn, advisor, plan, request_id=request_id)
    if not review.approved:
        delivery = pm.format_delivery(
            request,
            "I drafted a plan for this but it didn't pass review, so I'm not "
            "proceeding without a sound approach. Could you add detail or adjust "
            "the scope?",
        )
        return JobOutcome(
            status="plan_declined",
            job_id=job_id,
            request=request,
            plan_id=plan.id,
            delivery=delivery,
        )

    # 3) Boss starts the runner: plan Approved → InProgress.
    plans_repo.set_plan_status(conn, plan.id, "InProgress", actor=Role.boss)

    # 4) Run each phase in order; stop + escalate if one isn't signed off.
    for phase in plans_repo.list_phases(conn, plan.id):
        decision = _run_one_phase(
            conn, advisor, phase, request_id=request_id, job_id=job_id, user_id=user_id
        )
        if decision != "approve":
            delivery = pm.format_delivery(
                request,
                "I worked through this but a phase needs another look before I "
                "can finish. I'll follow up rather than deliver something unsound.",
            )
            return JobOutcome(
                status="phase_escalated",
                job_id=job_id,
                request=request,
                plan_id=plan.id,
                delivery=delivery,
            )

    # 5) Company Expert: plan InProgress → Resolved (all phases signed off).
    plans_repo.set_plan_status(conn, plan.id, "Resolved", actor=Role.company_expert)

    # 6) Plan Expert assembles the §9.2 final report.
    report = plan_expert.assemble_final_report(conn, plan)

    # 7) PM delivers the result to the user.
    delivery = pm.format_delivery(request, report.brief_description)
    return JobOutcome(
        status="completed",
        job_id=job_id,
        request=request,
        plan_id=plan.id,
        report=report,
        delivery=delivery,
    )
