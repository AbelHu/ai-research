"""Tests for Senior Worker task execution (implementation-plan T6.5)."""

from __future__ import annotations

import json

import pytest

from app.advisor.schemas import PhaseSpec, PlanSpec, TaskSpec
from app.advisor.wrapper import Advisor
from app.roles.envelope import Role
from app.roles.senior import _topological_order, run_phase
from app.skills.context import SkillContext
from app.storage.db import connect
from app.storage.migrations import migrate
from app.storage.repos import memories as memories_repo
from app.storage.repos import plans as plans_repo
from app.storage.repos import requests as requests_repo
from app.storage.repos import steps as steps_repo
from app.storage.repos.plans import PlanTask

# The worker proposes a read-only memory.search for every task.
SEARCH_ACTION = json.dumps(
    {"skill": "memory.search", "params": {"query": "vendor"}, "rationale": "look it up"}
)


@pytest.fixture
def db():
    conn = connect()
    migrate(conn)
    memories_repo.create_memory(conn, content="vendor comparison notes")
    try:
        yield conn
    finally:
        conn.close()


def _approved_plan(conn, spec: PlanSpec):
    req = requests_repo.create_request(conn, title="vendor compare")
    job = requests_repo.create_job(conn, request_id=req.id, kind="task", complexity="complex")
    plan = plans_repo.create_plan_from_spec(conn, job_id=job.id, spec=spec)
    plans_repo.approve_plan(conn, plan.id, actor=Role.company_expert)
    return req, job, plan


def _advisor(conn):
    from tests.fakes import FakeProvider

    return Advisor(resolve_provider=lambda role: FakeProvider(SEARCH_ACTION), conn=conn)


def test_dependent_tasks_run_in_order(db) -> None:
    spec = PlanSpec(
        phases=[
            PhaseSpec(
                title="Work",
                tasks=[
                    TaskSpec(title="A gather", depends_on=[]),
                    TaskSpec(title="B score", depends_on=[0]),  # depends on A
                    TaskSpec(title="C write", depends_on=[1]),  # depends on B
                ],
            )
        ]
    )
    req, job, plan = _approved_plan(db, spec)
    phase = plans_repo.list_phases(db, plan.id)[0]
    plans_repo.set_phase_status(db, phase.id, "Active", actor=Role.boss)

    runs = run_phase(db, _advisor(db), phase, request_id=req.id, job_id=job.id, user_id=None)

    # Execution respected the dependency chain A → B → C.
    titles = [plans_repo.get_task(db, r.task_id).title for r in runs]
    assert titles == ["A gather", "B score", "C write"]
    # Every task ended Resolved; the phase moved to InProgress.
    assert all(r.status == "Resolved" for r in runs)
    assert plans_repo.get_phase(db, phase.id).status == "InProgress"


def test_results_recorded_as_steps(db) -> None:
    spec = PlanSpec(phases=[PhaseSpec(title="P", tasks=[TaskSpec(title="do it")])])
    req, job, plan = _approved_plan(db, spec)
    phase = plans_repo.list_phases(db, plan.id)[0]
    plans_repo.set_phase_status(db, phase.id, "Active", actor=Role.boss)

    runs = run_phase(db, _advisor(db), phase, request_id=req.id, job_id=job.id, user_id=None)

    steps = steps_repo.list_steps(db, job.id)
    assert len(steps) == 1
    assert steps[0]["skill_name"] == "memory.search"
    assert steps[0]["plan_task_id"] == runs[0].task_id  # step linked to the task


def test_topological_order_detects_cycle() -> None:
    a = PlanTask(1, 1, None, "A", "Approved", "serial", [2], None, "t")
    b = PlanTask(2, 1, None, "B", "Approved", "serial", [1], None, "t")
    with pytest.raises(Exception):  # noqa: B017 - DependencyCycle
        _topological_order([a, b])


def test_independent_tasks_keep_id_order() -> None:
    tasks = [
        PlanTask(3, 1, None, "C", "Approved", "parallel", [], None, "t"),
        PlanTask(1, 1, None, "A", "Approved", "parallel", [], None, "t"),
        PlanTask(2, 1, None, "B", "Approved", "parallel", [], None, "t"),
    ]
    assert [t.id for t in _topological_order(tasks)] == [1, 2, 3]


def test_skill_context_has_task_id(db) -> None:
    # Sanity: the runtime links a step to the task via ctx.task_id.
    ctx = SkillContext(
        user_id=0, conn=db, permissions=frozenset({"memory.read"}), job_id=1, task_id=9
    )
    assert ctx.task_id == 9
