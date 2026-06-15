"""Tests for Analyzer plan drafting + persistence (implementation-plan T6.1)."""

from __future__ import annotations

import json

import pytest

from app.advisor.wrapper import Advisor
from app.roles.analyzer import draft_plan
from app.storage.db import connect
from app.storage.migrations import migrate
from app.storage.repos import plans as plans_repo
from app.storage.repos import requests as requests_repo
from tests.fakes import FakeProvider

PLAN_JSON = json.dumps(
    {
        "phases": [
            {
                "title": "Research",
                "tasks": [
                    {"title": "gather vendor A docs", "depends_on": [], "run_mode": "parallel"},
                    {"title": "gather vendor B docs", "depends_on": [], "run_mode": "parallel"},
                ],
            },
            {
                "title": "Compare",
                "tasks": [
                    {"title": "score on criteria", "depends_on": [], "run_mode": "serial"},
                    {"title": "write recommendation", "depends_on": [0], "run_mode": "serial"},
                ],
            },
        ]
    }
)


@pytest.fixture
def db():
    conn = connect()
    migrate(conn)
    try:
        yield conn
    finally:
        conn.close()


def _job(conn, *, kind="task"):
    req = requests_repo.create_request(conn, title="compare vendors")
    job = requests_repo.create_job(conn, request_id=req.id, kind=kind, complexity="complex")
    card = {
        "request_id": req.id,
        "request_code": req.code,
        "title": req.title,
        "text": "compare three vendors and recommend one",
        "append": False,
    }
    return job, card


def test_draft_plan_persists_phases_and_tasks(db) -> None:
    job, card = _job(db)
    advisor = Advisor(resolve_provider=lambda role: FakeProvider(PLAN_JSON), conn=db)

    plan = draft_plan(db, advisor, card, job_id=job.id)

    assert plan.status == "New"
    assert plans_repo.get_plan_for_job(db, job.id).id == plan.id

    phases = plans_repo.list_phases(db, plan.id)
    assert [p.title for p in phases] == ["Research", "Compare"]
    assert [p.idx for p in phases] == [0, 1]
    assert all(p.status == "New" for p in phases)

    research_tasks = plans_repo.list_tasks(db, phases[0].id)
    assert [t.title for t in research_tasks] == ["gather vendor A docs", "gather vendor B docs"]
    assert all(t.run_mode == "parallel" for t in research_tasks)
    assert all(t.owner_role == "senior_worker" for t in research_tasks)


def test_depends_on_indices_become_task_ids(db) -> None:
    job, card = _job(db)
    advisor = Advisor(resolve_provider=lambda role: FakeProvider(PLAN_JSON), conn=db)
    plan = draft_plan(db, advisor, card, job_id=job.id)

    compare = plans_repo.list_phases(db, plan.id)[1]
    tasks = plans_repo.list_tasks(db, compare.id)
    # "write recommendation" depends on task index 0 ("score on criteria") →
    # rewritten to that task's concrete id.
    assert tasks[1].depends_on == [tasks[0].id]
    assert tasks[0].depends_on == []


def test_invalid_dependency_index_rejected(db) -> None:
    job, card = _job(db)
    bad = json.dumps(
        {
            "phases": [
                {
                    "title": "P",
                    # task 0 depends on itself (index 0) → invalid (not earlier).
                    "tasks": [{"title": "t", "depends_on": [0], "run_mode": "serial"}],
                }
            ]
        }
    )
    advisor = Advisor(resolve_provider=lambda role: FakeProvider(bad), conn=db)
    with pytest.raises(ValueError, match="invalid index"):
        draft_plan(db, advisor, card, job_id=job.id)


def test_empty_plan_is_rejected_by_schema(db) -> None:
    job, card = _job(db)
    # phases must be non-empty (minItems 1); repeats so the repair also fails.
    advisor = Advisor(resolve_provider=lambda role: FakeProvider('{"phases": []}'), conn=db)
    with pytest.raises(Exception):  # noqa: B017 - AdvisorValidationError on escalate
        draft_plan(db, advisor, card, job_id=job.id)
