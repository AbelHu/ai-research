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
from app.config.policies import get_policies
from app.memory.reports import FinalReport
from app.roles import analyzer, company_expert, conversation, plan_expert, pm, senior
from app.roles.envelope import Role
from app.storage.repos import coder_queue as coder_queue_repo
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


def _completion_summary(conn, plan) -> str:
    """A short, id-free summary of what a plan accomplished (for the P3 check).

    Built from the plan's phases + task titles so the verifier judges criteria
    against the actual work breakdown, not internal ids.
    """
    lines: list[str] = []
    for phase in plans_repo.list_phases(conn, plan.id):
        tasks = plans_repo.list_tasks(conn, phase.id)
        task_titles = ", ".join(t.title or "task" for t in tasks) or "—"
        name = phase.title or f"phase {phase.idx}"
        lines.append(f"Phase '{name}' ({phase.status}): {task_titles}")
    return "\n".join(lines) if lines else "No phases were run."


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
    delivery_coords: dict | None = None,
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

    # 1-2) Draft + review the plan, with bounded auto-replan on a decline (P2#4):
    # the reviewer's comments feed the redraft so the next plan addresses them,
    # before we escalate to the user. The declined draft is abandoned each round.
    max_replans = get_policies().max_replan_attempts
    plan = analyzer.draft_plan(conn, advisor, card, job_id=job_id)
    review = company_expert.review_plan(conn, advisor, plan, request_id=request_id)
    replans = 0
    while not review.approved and replans < max_replans:
        replans += 1
        plans_repo.set_plan_status(conn, plan.id, "Abandoned", actor=Role.company_expert)
        feedback = "; ".join(review.verdict.comments) or "it did not pass review"
        replan_card = {
            **card,
            "context": (card.get("context") or "")
            + f"\n\nA previous plan was declined ({feedback}). "
            "Draft a revised plan that addresses that feedback.",
        }
        plan = analyzer.draft_plan(conn, advisor, replan_card, job_id=job_id)
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

    # A feature job's real deliverable is generated later in the dedicated coder
    # lane (step 6), which sandbox-validates it (import + lint + the model's own
    # tests). The phase-level criteria check below would run BEFORE that code
    # exists, so it can't meaningfully gate a feature here — the coder's sandbox
    # validation is the gate. Other job kinds still verify their criteria now.
    job_kind = requests_repo.get_job(conn, job_id).kind

    # 4b) Verify the goal's explicit success criteria before reporting done (P3).
    # Only gates when the plan carried criteria and the policy is on; an unmet
    # check escalates honestly rather than reporting a false completion.
    criteria_note: str | None = None
    stored_plan = plans_repo.get_plan(conn, plan.id)
    criteria = stored_plan.success_criteria if stored_plan else []
    if criteria and get_policies().verify_success_criteria and job_kind != "feature":
        verdict = advisor.verify_completion(
            goal=card["text"],
            criteria=criteria,
            summary=_completion_summary(conn, plan),
            request_id=request_id,
            job_id=job_id,
        )
        if not verdict.all_met:
            unmet = [r.criterion for r in verdict.results if not r.met] or criteria
            delivery = pm.format_delivery(
                request,
                "I worked through the plan but couldn't confirm it meets the goal "
                "yet — these criteria aren't satisfied: " + "; ".join(unmet) + ". "
                "I'll follow up rather than report it done.",
            )
            # Wait on the user: their next reply threads back here (§6C continuity).
            requests_repo.set_request_status(conn, request_id, requests_repo.AWAITING_STATUS)
            return JobOutcome(
                status="criteria_unmet",
                job_id=job_id,
                request=request,
                plan_id=plan.id,
                delivery=delivery,
            )
        met_count = sum(1 for r in verdict.results if r.met) or len(criteria)
        criteria_note = f"Verified {met_count}/{len(criteria)} success criteria met."

    # 5) Company Expert: plan InProgress → Resolved (all phases signed off).
    plans_repo.set_plan_status(conn, plan.id, "Resolved", actor=Role.company_expert)

    # 6) Feature jobs produce reusable code, but generation runs in the dedicated,
    # privileged **coder lane** (P4): enqueue one coding request here and let the
    # coder worker generate → sandbox-validate → promote inert, then deliver a
    # follow-up. The result stays gated on confirmation (`confirm_generated_code`).
    coder_note = ""
    if job_kind == "feature":
        coords = delivery_coords or {}
        coder_queue_repo.enqueue(
            conn,
            job_id=job_id,
            request_id=request_id,
            job_code=request.code,
            goal=card["text"],
            channel=coords.get("channel"),
            chat_id=coords.get("chat_id"),
            reply_to_message_id=coords.get("reply_to_message_id"),
            user_id=user_id,
        )
        coder_note = (
            "\n\nI'm building and verifying a reusable skill for this; I'll follow "
            "up when it's ready for you to confirm."
        )

    # 7) Plan Expert assembles the §9.2 final report.
    report = plan_expert.assemble_final_report(conn, plan, criteria_note=criteria_note)

    # 8) PM delivers the result to the user.
    delivery = pm.format_delivery(request, report.brief_description + coder_note)
    return JobOutcome(
        status="completed",
        job_id=job_id,
        request=request,
        plan_id=plan.id,
        report=report,
        delivery=delivery,
    )
