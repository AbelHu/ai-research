"""Tests for Plan Expert phase resolution + final report (plan T6.6)."""

from __future__ import annotations

import pytest

from app.advisor.schemas import PhaseSpec, PlanSpec, TaskSpec
from app.memory.reports import FinalReport
from app.roles.envelope import Role
from app.roles.plan_expert import all_phases_closed, assemble_final_report, resolve_phase
from app.storage.db import connect
from app.storage.migrations import migrate
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


def _plan(conn, spec):
    req = requests_repo.create_request(conn, title="vendor compare")
    job = requests_repo.create_job(conn, request_id=req.id, kind="task", complexity="complex")
    plan = plans_repo.create_plan_from_spec(conn, job_id=job.id, spec=spec)
    plans_repo.approve_plan(conn, plan.id, actor=Role.company_expert)
    return req, job, plan


def _run_tasks(conn, phase):
    plans_repo.set_phase_status(conn, phase.id, "Active", actor=Role.boss)
    plans_repo.set_phase_status(conn, phase.id, "InProgress", actor=Role.senior_worker)
    for task in plans_repo.list_tasks(conn, phase.id):
        plans_repo.set_task_status(conn, task.id, "InProgress", actor=Role.senior_worker)
        plans_repo.set_task_status(conn, task.id, "Resolved", actor=Role.senior_worker)


def test_all_tasks_resolved_resolves_phase_with_report(db) -> None:
    spec = PlanSpec(
        phases=[PhaseSpec(title="Research", tasks=[TaskSpec(title="a"), TaskSpec(title="b")])]
    )
    req, job, plan = _plan(db, spec)
    phase = plans_repo.list_phases(db, plan.id)[0]
    _run_tasks(db, phase)

    result = resolve_phase(db, plans_repo.get_phase(db, phase.id))

    assert result.resolved is True
    assert result.report_ref == f"phase-{phase.id}-report"
    refreshed = plans_repo.get_phase(db, phase.id)
    assert refreshed.status == "Resolved"
    assert refreshed.report_ref == result.report_ref


def test_phase_not_resolved_while_tasks_pending(db) -> None:
    spec = PlanSpec(
        phases=[PhaseSpec(title="Research", tasks=[TaskSpec(title="a"), TaskSpec(title="b")])]
    )
    req, job, plan = _plan(db, spec)
    phase = plans_repo.list_phases(db, plan.id)[0]
    plans_repo.set_phase_status(db, phase.id, "Active", actor=Role.boss)
    plans_repo.set_phase_status(db, phase.id, "InProgress", actor=Role.senior_worker)
    # Only the first task is resolved.
    tasks = plans_repo.list_tasks(db, phase.id)
    plans_repo.set_task_status(db, tasks[0].id, "InProgress", actor=Role.senior_worker)
    plans_repo.set_task_status(db, tasks[0].id, "Resolved", actor=Role.senior_worker)

    result = resolve_phase(db, plans_repo.get_phase(db, phase.id))
    assert result.resolved is False
    assert plans_repo.get_phase(db, phase.id).status == "InProgress"


def test_assemble_final_report_when_all_phases_closed(db) -> None:
    spec = PlanSpec(
        phases=[
            PhaseSpec(title="Research", tasks=[TaskSpec(title="a")]),
            PhaseSpec(title="Compare", tasks=[TaskSpec(title="b")]),
        ]
    )
    req, job, plan = _plan(db, spec)
    for phase in plans_repo.list_phases(db, plan.id):
        _run_tasks(db, phase)
        resolve_phase(db, plans_repo.get_phase(db, phase.id))
        # Company Expert signs off → phase + tasks Closed.
        plans_repo.set_phase_status(db, phase.id, "Closed", actor=Role.company_expert)
        for task in plans_repo.list_tasks(db, phase.id):
            plans_repo.set_task_status(db, task.id, "Closed", actor=Role.company_expert)

    assert all_phases_closed(db, plan) is True
    report = assemble_final_report(db, plan)
    assert isinstance(report, FinalReport)
    assert report.request_id == req.id
    assert report.kind == "task"
    assert "Research" in report.brief_description
    assert "Compare" in report.brief_description


def test_all_phases_closed_false_when_open(db) -> None:
    spec = PlanSpec(phases=[PhaseSpec(title="P", tasks=[TaskSpec(title="a")])])
    req, job, plan = _plan(db, spec)
    assert all_phases_closed(db, plan) is False  # phases are Approved, not Closed


def test_plan_persists_success_criteria(db) -> None:
    # The Analyzer's success_criteria round-trip through the plans repo (P3).
    spec = PlanSpec(
        phases=[PhaseSpec(title="P", tasks=[TaskSpec(title="a")])],
        success_criteria=["compares 3 vendors", "gives one recommendation"],
    )
    req, job, plan = _plan(db, spec)
    stored = plans_repo.get_plan(db, plan.id)
    assert stored.success_criteria == ["compares 3 vendors", "gives one recommendation"]


def test_plan_without_criteria_defaults_empty(db) -> None:
    spec = PlanSpec(phases=[PhaseSpec(title="P", tasks=[TaskSpec(title="a")])])
    req, job, plan = _plan(db, spec)
    assert plans_repo.get_plan(db, plan.id).success_criteria == []


def test_final_report_appends_criteria_note(db) -> None:
    spec = PlanSpec(phases=[PhaseSpec(title="P", tasks=[TaskSpec(title="a")])])
    req, job, plan = _plan(db, spec)
    report = assemble_final_report(db, plan, criteria_note="Verified 2/2 success criteria met.")
    assert "Verified 2/2 success criteria met." in report.brief_description

