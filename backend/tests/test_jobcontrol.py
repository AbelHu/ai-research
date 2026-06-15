"""Tests for pause / resume / abandon (implementation-plan T6.7).

Async coroutines driven via ``asyncio.run`` (no pytest-asyncio dependency).
"""

from __future__ import annotations

import asyncio

import pytest

from app.advisor.schemas import PhaseSpec, PlanSpec, TaskSpec
from app.roles.envelope import Role
from app.roles.jobcontrol import JobControl, abandon_tree, run_job
from app.roles.scheduler import JobScheduler
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


def _approved_plan(conn, n_phases=2):
    req = requests_repo.create_request(conn, title="job")
    job = requests_repo.create_job(conn, request_id=req.id, kind="task", complexity="complex")
    spec = PlanSpec(
        phases=[PhaseSpec(title=f"P{i}", tasks=[TaskSpec(title="t")]) for i in range(n_phases)]
    )
    plan = plans_repo.create_plan_from_spec(conn, job_id=job.id, spec=spec)
    plans_repo.approve_plan(conn, plan.id, actor=Role.company_expert)
    return req, job, plan


def test_pause_parks_holds_slot_then_resume_continues(db) -> None:
    async def body() -> None:
        req, job, plan = _approved_plan(db, n_phases=2)
        control = JobControl(db, job.id)
        processed: list[int] = []

        async def process_phase(phase) -> None:
            processed.append(phase.id)

        # Pause BEFORE running → the runner parks at the first checkpoint.
        control.pause()
        assert requests_repo.get_job(db, job.id).paused is True

        scheduler = JobScheduler(max_concurrent=3)
        scheduler.submit(job.id, lambda: run_job(control, db, plan.id, process_phase=process_phase))
        await asyncio.sleep(0)  # let it reach the checkpoint and park

        # Parked: no phase processed, yet the job still holds its slot.
        assert processed == []
        assert job.id in scheduler.running
        assert control.paused is True

        # Resume → the runner continues through all phases.
        control.resume()
        assert requests_repo.get_job(db, job.id).paused is False
        await asyncio.wait_for(scheduler.join(), timeout=1)

        assert len(processed) == 2  # both phases ran after resume

    asyncio.run(body())


def test_abandon_cancels_marks_tree_and_frees_slot(db) -> None:
    async def body() -> None:
        req, job, plan = _approved_plan(db, n_phases=2)
        control = JobControl(db, job.id)
        started = asyncio.Event()

        async def process_phase(phase) -> None:
            started.set()
            await asyncio.Event().wait()  # block forever → cancellable

        scheduler = JobScheduler(max_concurrent=3)
        scheduler.submit(job.id, lambda: run_job(control, db, plan.id, process_phase=process_phase))
        await asyncio.wait_for(started.wait(), timeout=1)
        assert job.id in scheduler.running

        # Abandon: dispose cancels the runner → its handler marks the tree.
        scheduler.dispose(job.id)
        await asyncio.wait_for(scheduler.join(), timeout=1)

        assert job.id not in scheduler.running  # slot freed
        assert plans_repo.get_plan(db, plan.id).status == "Abandoned"
        for phase in plans_repo.list_phases(db, plan.id):
            assert phase.status == "Abandoned"
            for task in plans_repo.list_tasks(db, phase.id):
                assert task.status == "Abandoned"

    asyncio.run(body())


def test_abandon_tree_skips_terminal_entities(db) -> None:
    req, job, plan = _approved_plan(db, n_phases=1)
    phase = plans_repo.list_phases(db, plan.id)[0]
    # Close one task first; abandon must leave it Closed (terminal).
    task = plans_repo.list_tasks(db, phase.id)[0]
    plans_repo.set_phase_status(db, phase.id, "Active", actor=Role.boss)
    plans_repo.set_phase_status(db, phase.id, "InProgress", actor=Role.senior_worker)
    plans_repo.set_task_status(db, task.id, "InProgress", actor=Role.senior_worker)
    plans_repo.set_task_status(db, task.id, "Resolved", actor=Role.senior_worker)
    plans_repo.set_task_status(db, task.id, "Closed", actor=Role.company_expert)

    abandon_tree(db, plan.id)

    assert plans_repo.get_task(db, task.id).status == "Closed"  # terminal untouched
    assert plans_repo.get_plan(db, plan.id).status == "Abandoned"


def test_pause_is_durable_in_db(db) -> None:
    async def body() -> None:
        req, job, plan = _approved_plan(db, n_phases=1)
        control = JobControl(db, job.id)
        control.pause()
        # A fresh read (as if after restart) sees the durable flag.
        assert requests_repo.get_job(db, job.id).paused is True
        control.resume()
        assert requests_repo.get_job(db, job.id).paused is False

    asyncio.run(body())
