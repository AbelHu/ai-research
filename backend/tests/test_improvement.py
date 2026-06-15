"""Tests for the improvement loop (implementation-plan T6.8)."""

from __future__ import annotations

import pytest

from app.advisor.schemas import PhaseSpec, PlanSpec, TaskSpec
from app.roles.envelope import Role
from app.roles.improvement import finish_job, improvement_chain_depth
from app.storage.db import connect
from app.storage.migrations import migrate
from app.storage.repos import library as library_repo
from app.storage.repos import plans as plans_repo
from app.storage.repos import requests as requests_repo


@pytest.fixture
def db():
    conn = connect()
    migrate(conn)
    try:
        yield conn
    finally:
        conn.close()


def _resolved_plan(conn):
    """A request + job + plan driven all the way to plan ``Resolved``."""
    req = requests_repo.create_request(conn, title="vendor compare")
    job = requests_repo.create_job(conn, request_id=req.id, kind="task", complexity="complex")
    spec = PlanSpec(phases=[PhaseSpec(title="P", tasks=[TaskSpec(title="t")])])
    plan = plans_repo.create_plan_from_spec(conn, job_id=job.id, spec=spec)
    plans_repo.approve_plan(conn, plan.id, actor=Role.company_expert)
    plans_repo.set_plan_status(conn, plan.id, "InProgress", actor=Role.boss)
    # close the single phase + task
    phase = plans_repo.list_phases(conn, plan.id)[0]
    plans_repo.set_phase_status(conn, phase.id, "Active", actor=Role.boss)
    plans_repo.set_phase_status(conn, phase.id, "InProgress", actor=Role.senior_worker)
    task = plans_repo.list_tasks(conn, phase.id)[0]
    plans_repo.set_task_status(conn, task.id, "InProgress", actor=Role.senior_worker)
    plans_repo.set_task_status(conn, task.id, "Resolved", actor=Role.senior_worker)
    plans_repo.set_phase_status(conn, phase.id, "Resolved", actor=Role.plan_expert)
    plans_repo.set_phase_status(conn, phase.id, "Closed", actor=Role.company_expert)
    plans_repo.set_task_status(conn, task.id, "Closed", actor=Role.company_expert)
    plans_repo.set_plan_status(conn, plan.id, "Resolved", actor=Role.company_expert)
    return req, job, plan


def test_decline_just_closes(db) -> None:
    req, job, plan = _resolved_plan(db)
    result = finish_job(db, request_id=req.id, plan_id=plan.id, confirm_improvement=False)

    assert result.closed is True
    assert result.spawned_request is None
    assert plans_repo.get_plan(db, plan.id).status == "Closed"
    assert requests_repo.get_request(db, req.id).state == "archived"


def test_confirm_spawns_linked_request_after_close(db) -> None:
    req, job, plan = _resolved_plan(db)
    result = finish_job(db, request_id=req.id, plan_id=plan.id, confirm_improvement=True)

    # Original is closed + archived FIRST.
    assert plans_repo.get_plan(db, plan.id).status == "Closed"
    assert requests_repo.get_request(db, req.id).state == "archived"
    # A new linked improvement request was spawned.
    assert result.spawned_request is not None
    assert result.spawned_request.improves_request_id == req.id
    assert result.spawned_request.id != req.id


def test_confirm_updates_final_report_links(db) -> None:
    req, job, plan = _resolved_plan(db)
    fr_id = library_repo.create_final_report(db, request_id=req.id, job_id=job.id)

    result = finish_job(
        db,
        request_id=req.id,
        plan_id=plan.id,
        confirm_improvement=True,
        final_report_id=fr_id,
    )
    row = library_repo.get_final_report(db, fr_id)
    assert row["user_confirmed"] == 1
    assert row["spawned_request_id"] == result.spawned_request.id


def test_iteration_cap_blocks_further_improvements(db) -> None:
    # Build a chain at the cap depth, then confirming should NOT spawn.
    cap = 2
    origin = requests_repo.create_request(db, title="orig")
    prev_id = origin.id
    for _ in range(cap):
        nxt = requests_repo.create_request(db, title="imp", improves_request_id=prev_id)
        prev_id = nxt.id
    # prev_id is now at chain depth == cap.
    job = requests_repo.create_job(db, request_id=prev_id, kind="task", complexity="complex")
    spec = PlanSpec(phases=[PhaseSpec(title="P", tasks=[TaskSpec(title="t")])])
    plan = plans_repo.create_plan_from_spec(db, job_id=job.id, spec=spec)
    plans_repo.approve_plan(db, plan.id, actor=Role.company_expert)
    plans_repo.set_plan_status(db, plan.id, "InProgress", actor=Role.boss)
    plans_repo.set_plan_status(db, plan.id, "Resolved", actor=Role.company_expert)

    assert improvement_chain_depth(db, prev_id) == cap
    result = finish_job(
        db,
        request_id=prev_id,
        plan_id=plan.id,
        confirm_improvement=True,
        max_improvement_iterations=cap,
    )
    assert result.capped is True
    assert result.spawned_request is None
    assert plans_repo.get_plan(db, plan.id).status == "Closed"  # still closes


def test_chain_depth_counts_links(db) -> None:
    a = requests_repo.create_request(db, title="a")
    b = requests_repo.create_request(db, title="b", improves_request_id=a.id)
    c = requests_repo.create_request(db, title="c", improves_request_id=b.id)
    assert improvement_chain_depth(db, a.id) == 0
    assert improvement_chain_depth(db, b.id) == 1
    assert improvement_chain_depth(db, c.id) == 2
