"""Tests for Company Expert sign-off (implementation-plan T6.3)."""

from __future__ import annotations

import json

import pytest

from app.advisor.schemas import PhaseSpec, PlanSpec, TaskSpec
from app.advisor.wrapper import Advisor
from app.roles.company_expert import review_phase, review_plan
from app.roles.envelope import Role
from app.storage.db import connect
from app.storage.migrations import migrate
from app.storage.repos import plans as plans_repo
from app.storage.repos import requests as requests_repo
from tests.fakes import FakeProvider

APPROVE = json.dumps({"decision": "approve", "comments": []})
DECLINE = json.dumps({"decision": "decline", "comments": ["add error handling"]})


@pytest.fixture
def db():
    conn = connect()
    migrate(conn)
    try:
        yield conn
    finally:
        conn.close()


def _plan(conn):
    req = requests_repo.create_request(conn, title="vendor compare")
    job = requests_repo.create_job(conn, request_id=req.id, kind="task", complexity="complex")
    spec = PlanSpec(
        phases=[
            PhaseSpec(title="Research", tasks=[TaskSpec(title="gather")]),
            PhaseSpec(title="Compare", tasks=[TaskSpec(title="score")]),
        ]
    )
    plan = plans_repo.create_plan_from_spec(conn, job_id=job.id, spec=spec)
    return req, job, plan


def _advisor(conn, canned):
    return Advisor(resolve_provider=lambda role: FakeProvider(canned), conn=conn)


def test_approve_plan_cascades_to_approved(db) -> None:
    req, job, plan = _plan(db)
    review = review_plan(db, _advisor(db, APPROVE), plan, request_id=req.id)

    assert review.approved is True
    assert plans_repo.get_plan(db, plan.id).status == "Approved"
    assert plans_repo.get_plan(db, plan.id).approved_by == "company_expert"
    for phase in plans_repo.list_phases(db, plan.id):
        assert phase.status == "Approved"
        for task in plans_repo.list_tasks(db, phase.id):
            assert task.status == "Approved"


def test_decline_plan_leaves_it_new(db) -> None:
    req, job, plan = _plan(db)
    review = review_plan(db, _advisor(db, DECLINE), plan, request_id=req.id)
    assert review.approved is False
    assert review.verdict.comments == ["add error handling"]
    assert plans_repo.get_plan(db, plan.id).status == "New"


def _resolved_phase(db, plan):
    """Drive a plan's first phase to Resolved so it can be reviewed."""
    phase = plans_repo.list_phases(db, plan.id)[0]
    plans_repo.approve_plan(db, plan.id, actor=Role.company_expert)
    plans_repo.set_phase_status(db, phase.id, "Active", actor=Role.boss)
    plans_repo.set_phase_status(db, phase.id, "InProgress", actor=Role.senior_worker)
    for task in plans_repo.list_tasks(db, phase.id):
        plans_repo.set_task_status(db, task.id, "InProgress", actor=Role.senior_worker)
        plans_repo.set_task_status(db, task.id, "Resolved", actor=Role.senior_worker)
    plans_repo.set_phase_status(db, phase.id, "Resolved", actor=Role.plan_expert)
    return plans_repo.get_phase(db, phase.id)


def _force_phase_resolved(db, phase_id):
    """Test precondition: put a phase back into Resolved (bypassing lifecycle)."""
    db.execute("UPDATE phases SET status = 'Resolved' WHERE id = ?", (phase_id,))
    db.commit()
    return plans_repo.get_phase(db, phase_id)


def test_approve_phase_closes_phase_and_tasks(db) -> None:
    req, job, plan = _plan(db)
    phase = _resolved_phase(db, plan)
    review = review_phase(db, _advisor(db, APPROVE), phase, request_id=req.id, job_id=job.id)

    assert review.decision == "approve"
    assert review.escalate is False
    assert plans_repo.get_phase(db, phase.id).status == "Closed"
    for task in plans_repo.list_tasks(db, phase.id):
        assert task.status == "Closed"


def test_decline_loops_then_escalates_at_cap(db) -> None:
    req, job, plan = _plan(db)
    advisor = _advisor(db, DECLINE)
    cap = 3
    phase = _resolved_phase(db, plan)

    # Each decline (while under the cap) reactivates the phase + bumps the count.
    for expected_count in range(1, cap + 1):
        review = review_phase(
            db, advisor, phase, request_id=req.id, job_id=job.id, max_phase_declines=cap
        )
        assert review.escalate is False
        refreshed = plans_repo.get_phase(db, phase.id)
        assert refreshed.status == "Active"
        assert refreshed.decline_count == expected_count
        phase = _force_phase_resolved(db, phase.id)  # Plan Expert re-resolves after rework

    # The (cap+1)-th decline escalates instead of looping; phase stays Resolved.
    review = review_phase(
        db, advisor, phase, request_id=req.id, job_id=job.id, max_phase_declines=cap
    )
    assert review.escalate is True
    assert plans_repo.get_phase(db, phase.id).status == "Resolved"
