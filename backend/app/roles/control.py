"""Synchronous control loop for a simple ask (design-spec §6, §6D; T4.6/T4.7).

Drives one request through the company roles **synchronously**, persisting every
hand-off as a `role_messages` envelope (the durable, recoverable trace, §6D):

    PM (first-pass) → Boss → Analyzer → Boss → Junior Worker → Boss → PM (deliver)

The Boss is the only router; each role returns a typed result + the envelope it
emits. The loop persists envelopes with a `causation_id` chain so the whole run
is reconstructable from the DB (recovery, T4.7). No Telegram, no async, no
per-job runner yet — those arrive in P6/P8.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from app.advisor.schemas import AnswerDraft
from app.advisor.wrapper import Advisor
from app.roles import analyzer, boss, junior, pm
from app.roles.envelope import Action, Role, RoleMessage
from app.storage.repos import requests as requests_repo
from app.storage.repos import role_messages as role_messages_repo
from app.storage.repos.identities import ensure_owner
from app.storage.repos.requests import Request

# Owner resolution lives in the identities repo (§10.1); re-exported here so the
# P4 callers (`run_ask`, the `ask` CLI, tests) keep importing it from control.
__all__ = ["AskOutcome", "AskStatus", "ensure_owner", "run_ask"]

AskStatus = Literal["answered", "unanswered", "needs_clarification", "planned", "rejected"]


@dataclass(frozen=True)
class AskOutcome:
    """The terminal result of `run_ask` (what the PM would surface to the user)."""

    status: AskStatus
    request: Request
    job_id: int | None = None
    answer: AnswerDraft | None = None
    clarify: list[str] | None = None
    delivery: str | None = None  # PM-formatted user message (answered path)


def _emit_boss(
    conn,
    decision: boss.BossDecision,
    *,
    request_id: int,
    job_id: int | None,
    payload: dict,
    causation_id: int,
) -> int:
    """Persist the Boss's scheduling envelope (Boss → next role)."""
    return role_messages_repo.record_envelope(
        conn,
        RoleMessage(
            request_id=request_id,
            job_id=job_id,
            from_role=Role.boss,
            to_role=decision.to_role,
            action=decision.action,
            payload=payload,
        ),
        causation_id=causation_id,
    )


def run_ask(conn, advisor: Advisor, text: str, *, user_id: int | None = None) -> AskOutcome:
    """Drive one inbound message end-to-end, returning the terminal outcome."""
    # 1) PM first-pass routing → route_request (PM → Boss).
    route = pm.route_inbound(conn, text, user_id=user_id)
    route_id = role_messages_repo.record_envelope(conn, route.envelope)
    request = route.request

    # 2) Boss routes route_request → analyze (Boss → Analyzer).
    analyze_decision = boss.decide(route.envelope)
    analyze_id = _emit_boss(
        conn,
        analyze_decision,
        request_id=request.id,
        job_id=None,
        payload=route.card,
        causation_id=route_id,
    )

    # 3) Analyzer validates + classifies → analysis_done (Analyzer → Boss).
    result = analyzer.analyze(conn, advisor, route.card)
    analysis_id = role_messages_repo.record_envelope(conn, result.envelope, causation_id=analyze_id)

    # 4) Boss routes the verdict.
    decision = boss.decide(result.envelope)

    if decision.action is Action.answer_ask:
        return _run_answer_path(
            conn,
            advisor,
            decision,
            route=route,
            result=result,
            user_id=user_id,
            causation_id=analysis_id,
        )

    if decision.action is Action.clarify:
        _emit_boss(
            conn,
            decision,
            request_id=request.id,
            job_id=None,
            payload={"clarify": result.analysis.clarify},
            causation_id=analysis_id,
        )
        return AskOutcome(
            status="needs_clarification", request=request, clarify=result.analysis.clarify
        )

    if decision.action is Action.review_plan:
        # Complex job: classification + job exist; execution is P6.
        _emit_boss(
            conn,
            decision,
            request_id=request.id,
            job_id=result.job_id,
            payload={"card": route.card},
            causation_id=analysis_id,
        )
        return AskOutcome(status="planned", request=request, job_id=result.job_id)

    # decision.action is Action.undo_append (wrong association).
    _emit_boss(
        conn,
        decision,
        request_id=request.id,
        job_id=None,
        payload={"card": route.card},
        causation_id=analysis_id,
    )
    return AskOutcome(status="rejected", request=request)


def _run_answer_path(
    conn,
    advisor: Advisor,
    decision: boss.BossDecision,
    *,
    route: pm.RouteResult,
    result: analyzer.AnalyzerResult,
    user_id: int | None,
    causation_id: int,
) -> AskOutcome:
    """Boss → Junior (answer_ask) for a simple ask.

    On a validated answer: Boss → PM (deliver). When the Junior can't answer, the
    ask is escalated into the planned-job path instead of dead-ending (§6A) —
    see :func:`_escalate_unanswerable_ask`.
    """
    request = route.request
    job_id = result.job_id
    assert job_id is not None  # the work path always minted a job

    # Boss schedules the Junior Worker.
    answer_ask_id = _emit_boss(
        conn,
        decision,
        request_id=request.id,
        job_id=job_id,
        payload={"card": route.card},
        causation_id=causation_id,
    )

    # Junior Worker: search → validated answer → ask_done (Junior → Boss).
    junior_result = junior.answer_ask(conn, advisor, route.card, user_id=user_id, job_id=job_id)
    ask_done_id = role_messages_repo.record_envelope(
        conn, junior_result.envelope, causation_id=answer_ask_id
    )

    # No citable answer (nothing in memory / no skill could answer it): rather
    # than dead-ending, hand it back to be **planned + executed** like a complex
    # job (§6A — the Junior hands non-trivial work to the Analyzer). The PM
    # delivers the result later, asynchronously, as a progress update.
    if junior_result.answer is None:
        return _escalate_unanswerable_ask(
            conn, route=route, request=request, job_id=job_id, causation_id=ask_done_id
        )

    # Validated answer → Boss routes ask_done → deliver (Boss → PM).
    deliver_decision = boss.decide(junior_result.envelope)
    delivery = pm.format_delivery(
        request, junior_result.answer.answer, sources=junior_result.answer.citations
    )
    _emit_boss(
        conn,
        deliver_decision,
        request_id=request.id,
        job_id=job_id,
        payload={"answer": junior_result.answer.model_dump()},
        causation_id=ask_done_id,
    )
    return AskOutcome(
        status="answered",
        request=request,
        job_id=job_id,
        answer=junior_result.answer,
        delivery=delivery,
    )


def _escalate_unanswerable_ask(
    conn,
    *,
    route: pm.RouteResult,
    request: Request,
    job_id: int,
    causation_id: int,
) -> AskOutcome:
    """Re-route a simple ask the Junior couldn't answer into the planned-job path.

    Per §6A the Junior Worker "hands anything non-trivial to the Analyzer for
    authoritative classification + planning." So we **promote** the misclassified
    ask to a ``task`` and route it to plan review — making it indistinguishable
    from a natively-complex job, so the per-job runner (which plans, calls or
    implements skills, and signs off) treats both uniformly. The answer is
    delivered later as a PM progress update (async), not in this turn.
    """
    requests_repo.set_job_kind(conn, job_id, "task")

    # Boss routes the escalation to plan review (same as the `plan_ready` verdict).
    decision = boss.BossDecision(Role.company_expert, Action.review_plan)
    _emit_boss(
        conn,
        decision,
        request_id=request.id,
        job_id=job_id,
        payload={"escalated_from": "ask", "card": route.card},
        causation_id=causation_id,
    )
    return AskOutcome(status="planned", request=request, job_id=job_id)
