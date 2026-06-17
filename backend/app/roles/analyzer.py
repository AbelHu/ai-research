"""The Analyzer — authoritative routing validation + classification (§6A, §6C, §6D).

After the Boss dispatches a request, the Analyzer (implementation-plan T4.4):

  1. calls the **advisor** to validate + classify it into a typed `Analysis`
     (kind / clarity / complexity, and — for an append — whether it *belongs*);
  2. maps that verdict deterministically to one of `append_rejected` /
     `ask_clarify` / `answer_ask` / `plan_ready`; and
  3. for the work paths (`answer_ask` / `plan_ready`) creates the **job** that
     carries the kind, then emits `analysis_done` to the Boss.

The model only advises; **code** picks the verdict and bounds the append-reject
retry (`max_append_reroutes`), keeping AI out of the control path (§6C).
"""

from __future__ import annotations

from dataclasses import dataclass

from app.advisor.schemas import Analysis
from app.advisor.wrapper import Advisor
from app.config.policies import get_policies
from app.roles.envelope import Action, Role, RoleMessage
from app.storage.repos import requests as requests_repo

# Verdicts that proceed to real work (and therefore mint a job).
_WORK_VERDICTS = {"answer_ask", "plan_ready"}


@dataclass(frozen=True)
class AnalyzerResult:
    analysis: Analysis
    verdict: str
    job_id: int | None
    envelope: RoleMessage  # the `analysis_done` hand-off to the Boss


def _verdict(analysis: Analysis, *, append: bool, reroute_count: int, max_reroutes: int) -> str:
    """Map a validated `Analysis` to a routing verdict (deterministic)."""
    if append and not analysis.belongs:
        # A wrong append → reject for re-association, bounded by the retry cap;
        # once exhausted, defer to the user (§6C).
        return "append_rejected" if reroute_count < max_reroutes else "ask_clarify"
    if analysis.clarity == "unclear" or analysis.clarify:
        return "ask_clarify"
    if analysis.kind == "ask":
        return "answer_ask"
    return "plan_ready"


def analyze(
    conn,
    advisor: Advisor,
    card: dict,
    *,
    reroute_count: int = 0,
    max_append_reroutes: int | None = None,
) -> AnalyzerResult:
    """Validate + classify a dispatched request, emitting `analysis_done` (§6C)."""
    max_reroutes = (
        max_append_reroutes
        if max_append_reroutes is not None
        else get_policies().max_append_reroutes
    )
    request_id = card["request_id"]
    append = bool(card.get("append", False))

    analysis = advisor.analyze(
        text=card["text"],
        title=card.get("title") or "",
        append=append,
        context=card.get("context") or "",
        request_id=request_id,
    )

    verdict = _verdict(
        analysis, append=append, reroute_count=reroute_count, max_reroutes=max_reroutes
    )

    job_id: int | None = None
    if verdict in _WORK_VERDICTS:
        job = requests_repo.create_job(
            conn,
            request_id=request_id,
            kind=analysis.kind,
            clarity=analysis.clarity,
            complexity=analysis.complexity,
        )
        job_id = job.id

    payload = {
        "verdict": verdict,
        "kind": analysis.kind,
        "clarity": analysis.clarity,
        "complexity": analysis.complexity,
        "job_id": job_id,
        "clarify": analysis.clarify,
        "card": card,
    }
    envelope = RoleMessage(
        request_id=request_id,
        job_id=job_id,
        from_role=Role.analyzer,
        to_role=Role.boss,
        action=Action.analysis_done,
        payload=payload,
        template="analyzer.analyze@v1",
    )
    return AnalyzerResult(analysis=analysis, verdict=verdict, job_id=job_id, envelope=envelope)


def draft_plan(conn, advisor: Advisor, card: dict, *, job_id: int):
    """Draft + persist a complex job's plan (§6B; implementation-plan T6.1).

    Calls the advisor for a validated `PlanSpec`, then persists it as a ``New``
    plan → phases → tasks tree (status transitions come later via sign-off).
    Returns the stored `Plan`.
    """
    from app.storage.repos import plans as plans_repo

    spec = advisor.make_plan(
        goal=card["text"],
        context=card.get("context") or "",
        request_id=card["request_id"],
        job_id=job_id,
    )
    return plans_repo.create_plan_from_spec(conn, job_id=job_id, spec=spec)
