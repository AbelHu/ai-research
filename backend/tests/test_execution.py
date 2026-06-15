"""Tests for per-job execution orchestration (`app.roles.execution`).

Offline: the full plan → phases → report run is driven with a single per-role
`FakeProvider` replaying the advisor calls in order (make_plan → review_plan →
[next_action → review_phase]* ). Pins the §6B status transitions end-to-end and
the honest stop-points (plan declined, phase escalated).
"""

from __future__ import annotations

import json

import pytest

from app.advisor.wrapper import Advisor
from app.roles.control import ensure_owner
from app.roles.execution import card_for_job, execute_planned_job
from app.storage.db import connect
from app.storage.migrations import migrate
from app.storage.repos import plans as plans_repo
from app.storage.repos import requests as requests_repo
from tests.fakes import FakeProvider


def _plan_json(*phase_titles: str) -> str:
    return json.dumps(
        {
            "phases": [
                {
                    "title": t,
                    "tasks": [{"title": f"do {t}", "depends_on": [], "run_mode": "serial"}],
                }
                for t in phase_titles
            ]
        }
    )


APPROVE = json.dumps({"decision": "approve", "comments": []})
DECLINE = json.dumps({"decision": "decline", "comments": ["needs work"]})
SEARCH = json.dumps({"skill": "memory.search", "params": {"query": "x"}, "rationale": "look it up"})


@pytest.fixture
def conn():
    c = connect()
    migrate(c)
    try:
        yield c
    finally:
        c.close()


def _advisor(conn, responses: list[str]) -> Advisor:
    # Every orchestrated advisor call resolves to role "planner"; one ordered
    # FakeProvider replays them in sequence.
    provider = FakeProvider(responses)
    return Advisor(resolve_provider=lambda _role: provider, conn=conn)


def _planned_job(conn, *, kind="task"):
    req = requests_repo.create_request(conn, title="compare three vendors")
    job = requests_repo.create_job(conn, request_id=req.id, kind=kind, complexity="complex")
    card = {
        "request_id": req.id,
        "request_code": req.code,
        "title": req.title,
        "text": "compare three vendors and recommend one",
        "append": False,
    }
    return req, job, card


def test_executes_plan_end_to_end_and_delivers(conn) -> None:
    ensure_owner(conn)
    req, job, card = _planned_job(conn)
    # make_plan(2 phases) → review_plan(approve) → [next_action, review_phase]×2.
    advisor = _advisor(
        conn, [_plan_json("Research", "Compare"), APPROVE, SEARCH, APPROVE, SEARCH, APPROVE]
    )

    outcome = execute_planned_job(
        conn, advisor, job_id=job.id, card=card, user_id=ensure_owner(conn)
    )

    assert outcome.status == "completed"
    assert outcome.report is not None
    assert outcome.delivery is not None and f"/req {req.code}" in outcome.delivery

    # §6B terminal statuses: plan Resolved, every phase + task Closed.
    plan = plans_repo.get_plan_for_job(conn, job.id)
    assert plan.status == "Resolved"
    phases = plans_repo.list_phases(conn, plan.id)
    assert [p.title for p in phases] == ["Research", "Compare"]
    assert all(p.status == "Closed" for p in phases)
    for phase in phases:
        assert all(t.status == "Closed" for t in plans_repo.list_tasks(conn, phase.id))


def test_plan_declined_stops_and_reports(conn) -> None:
    req, job, card = _planned_job(conn)
    advisor = _advisor(conn, [_plan_json("Only"), DECLINE])  # plan review declines

    outcome = execute_planned_job(conn, advisor, job_id=job.id, card=card)

    assert outcome.status == "plan_declined"
    assert outcome.delivery is not None and "didn't pass review" in outcome.delivery
    # The plan stayed New (never approved); nothing executed.
    plan = plans_repo.get_plan_for_job(conn, job.id)
    assert plan.status == "New"


def test_phase_decline_escalates(conn) -> None:
    req, job, card = _planned_job(conn)
    # plan approved, task runs, but the phase sign-off declines → escalate.
    advisor = _advisor(conn, [_plan_json("Only"), APPROVE, SEARCH, DECLINE])

    outcome = execute_planned_job(conn, advisor, job_id=job.id, card=card)

    assert outcome.status == "phase_escalated"
    assert outcome.delivery is not None and "needs another look" in outcome.delivery


def test_card_for_job_reconstructs_from_db(conn) -> None:
    req = requests_repo.create_request(conn, title="my request title")
    job = requests_repo.create_job(conn, request_id=req.id, kind="task", complexity="complex")

    card = card_for_job(conn, job.id)
    assert card["request_id"] == req.id
    assert card["title"] == "my request title"
    assert card["append"] is False


def test_execute_reconstructs_card_when_omitted(conn) -> None:
    req, job, _card = _planned_job(conn)
    advisor = _advisor(conn, [_plan_json("Only"), APPROVE, SEARCH, APPROVE])

    # No card passed → execute rebuilds it from the job (background-runner path).
    outcome = execute_planned_job(conn, advisor, job_id=job.id)

    assert outcome.status == "completed"
    assert outcome.plan_id is not None
