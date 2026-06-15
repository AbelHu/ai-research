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
from app.storage.repos import role_messages as role_messages_repo
from app.storage.repos.requests import Request

# Hardcoded single owner for the CLI harness (P4). Real pairing arrives in P7.
OWNER_DISPLAY_NAME = "owner"

AskStatus = Literal["answered", "needs_clarification", "planned", "rejected"]


@dataclass(frozen=True)
class AskOutcome:
    """The terminal result of `run_ask` (what the PM would surface to the user)."""

    status: AskStatus
    request: Request
    job_id: int | None = None
    answer: AnswerDraft | None = None
    clarify: list[str] | None = None
    delivery: str | None = None  # PM-formatted user message (answered path)


def ensure_owner(conn) -> int:
    """Return the single owner user's id, creating it on first use (P4)."""
    row = conn.execute("SELECT id FROM users WHERE is_owner = 1 ORDER BY id LIMIT 1").fetchone()
    if row is not None:
        return int(row["id"])
    with conn:
        cur = conn.execute(
            "INSERT INTO users (display_name, is_owner) VALUES (?, 1)",
            (OWNER_DISPLAY_NAME,),
        )
    return int(cur.lastrowid)


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
    """Boss → Junior (answer_ask) → Boss → PM (deliver) for a simple ask."""
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

    # Boss routes ask_done → deliver (Boss → PM); the PM surfaces it to the user.
    deliver_decision = boss.decide(junior_result.envelope)
    delivery = pm.format_delivery(request, junior_result.answer.answer)
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
