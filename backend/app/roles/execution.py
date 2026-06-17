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

import logging
from dataclasses import dataclass

from app.advisor.wrapper import Advisor
from app.memory.reports import FinalReport
from app.roles import analyzer, coder, company_expert, conversation, plan_expert, pm, senior
from app.roles.envelope import Role
from app.storage.repos import plans as plans_repo
from app.storage.repos import requests as requests_repo
from app.storage.repos.requests import Request

logger = logging.getLogger("app.roles.execution")

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
    generated_skill: coder.CoderResult | None = None  # feature jobs only


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
) -> company_expert.PhaseReview:
    """Drive one phase Approved → Closed; return the sign-off review.

    Boss activates the phase, the Senior Worker runs its tasks, the Plan Expert
    resolves it, and the Company Expert signs it off — each step a §6B-legal
    transition. Returns ``"approve"`` (phase Closed) or ``"decline"``.
    """
    # Boss: phase Approved → Active.
    current = plans_repo.get_phase(conn, phase.id)
    if current is not None and current.status == "Approved":
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
    return review


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

    # Rebuild the prior-turn context (the background runner picks the job up by id,
    # so the control loop's context isn't in hand). Lets a plan that refers to
    # earlier info — e.g. "the gold-price URL from before" — ground it (§6C).
    if not card.get("context"):
        prior = (
            requests_repo.get_latest_active_request(
                conn, request.user_id, exclude_request_id=request.id
            )
            if request.user_id is not None
            else None
        )
        card = {**card, "context": conversation.render(conversation.build(conn, prior))}

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
        # Wait on the user: their next reply threads back here (§6C continuity).
        requests_repo.set_request_status(conn, request_id, requests_repo.AWAITING_STATUS)
        return JobOutcome(
            status="plan_declined",
            job_id=job_id,
            request=request,
            plan_id=plan.id,
            delivery=delivery,
        )

    # 3) Boss starts the runner: plan Approved → InProgress.
    plans_repo.set_plan_status(conn, plan.id, "InProgress", actor=Role.boss)

    # 4) Run each phase in order. A recoverable decline loops for rework until
    # approved; only a capped decline escalates.
    for phase in plans_repo.list_phases(conn, plan.id):
        while True:
            review = _run_one_phase(
                conn, advisor, phase, request_id=request_id, job_id=job_id, user_id=user_id
            )
            if review.decision == "approve":
                break
            if review.escalate:
                delivery = pm.format_delivery(
                    request,
                    "I worked through this but a phase needs another look before I "
                    "can finish. I'll follow up rather than deliver something unsound.",
                )
                # Wait on the user: their next reply threads back here (§6C continuity).
                requests_repo.set_request_status(conn, request_id, requests_repo.AWAITING_STATUS)
                return JobOutcome(
                    status="phase_escalated",
                    job_id=job_id,
                    request=request,
                    plan_id=plan.id,
                    delivery=delivery,
                )

    # 5) Company Expert: plan InProgress → Resolved (all phases signed off).
    plans_repo.set_plan_status(conn, plan.id, "Resolved", actor=Role.company_expert)

    # 6) Feature jobs produce reusable code: the Coder generates the skill and it
    # is written **inert** (never executed) — activation is gated on user
    # confirmation (`confirm_generated_code`, §5/§6B). A generation failure is
    # non-fatal: the job still completes + reports (the code just isn't offered).
    generated = None
    if requests_repo.get_job(conn, job_id).kind == "feature":
        generated = _try_generate_skill(conn, advisor, job_id=job_id, goal=card["text"])

    # 7) Plan Expert assembles the §9.2 final report.
    report = plan_expert.assemble_final_report(conn, plan)

    # 8) PM delivers the result to the user (noting any code awaiting confirmation).
    message = report.brief_description
    if generated is not None:
        message += (
            f"\n\nI also built a reusable skill `{generated.skill_name}` for this. "
            "It's saved but inactive pending your review — confirm to activate it."
        )
    delivery = pm.format_delivery(request, message)
    return JobOutcome(
        status="completed",
        job_id=job_id,
        request=request,
        plan_id=plan.id,
        report=report,
        delivery=delivery,
        generated_skill=generated,
    )


def _try_generate_skill(conn, advisor: Advisor, *, job_id: int, goal: str):
    """Generate a feature job's skill (inert), swallowing failures as non-fatal.

    Codegen is a best-effort deliverable: if the model can't produce a valid
    skill, the job still completes and reports — we just don't offer code. We
    only ever write **inert** code here; the ``confirm_generated_code`` gate is
    honored downstream at activation (`codegen.confirm_and_activate`), so a
    generated skill never runs until the user confirms it (§5/§6B).
    """
    try:
        return coder.generate_feature_skill(conn, advisor, job_id=job_id, goal=goal)
    except Exception as exc:  # noqa: BLE001 - codegen is a non-fatal deliverable
        logger.warning("feature skill generation failed for job %s: %s", job_id, exc)
        return None
