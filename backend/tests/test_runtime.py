"""Tests for the skill runtime execute() pipeline (implementation-plan T2.4)."""

from __future__ import annotations

import json

import pytest
from pydantic import BaseModel, Field

from app.skills.context import SkillContext
from app.skills.policy import PermissionDenied
from app.skills.registry import skill
from app.skills.runtime import InvalidParams, SkillResult, UnknownSkill, execute
from app.storage.db import connect
from app.storage.migrations import migrate
from app.storage.repos import requests as requests_repo
from app.storage.repos import steps as steps_repo


class _EchoParams(BaseModel):
    text: str = Field(..., min_length=1)


class _EchoResult(BaseModel):
    echoed: str


def _register_echo(name: str = "test.echo", *, effect: str = "read", permissions=("x.read",)):
    @skill(
        name=name,
        description="echo the input",
        params=_EchoParams,
        returns=_EchoResult,
        permissions=list(permissions),
        effect=effect,
    )
    def _echo(params: _EchoParams, ctx: SkillContext) -> _EchoResult:
        return _EchoResult(echoed=params.text)


@pytest.fixture
def job_ctx():
    conn = connect()
    migrate(conn)
    req = requests_repo.create_request(conn)
    job = requests_repo.create_job(conn, request_id=req.id, kind="ask")
    ctx = SkillContext(
        user_id=1,
        conn=conn,
        permissions=frozenset({"x.read"}),
        job_id=job.id,
    )
    try:
        yield ctx
    finally:
        conn.close()


def test_happy_path_runs_and_records_a_step(skill_registry, job_ctx) -> None:
    _register_echo()
    result = execute("test.echo", {"text": "hi"}, job_ctx)

    assert isinstance(result, SkillResult)
    assert result.ok
    assert result.value == _EchoResult(echoed="hi")

    steps = steps_repo.list_steps(job_ctx.conn, job_ctx.job_id)
    assert len(steps) == 1
    row = steps[0]
    assert row["skill_name"] == "test.echo"
    assert row["status"] == "ok"
    assert json.loads(row["params_json"]) == {"text": "hi"}
    assert json.loads(row["result_json"]) == {"echoed": "hi"}
    assert json.loads(row["provenance_json"])["skill"] == "test.echo"
    assert result.step_id == row["id"]


def test_unknown_skill_raises(skill_registry, job_ctx) -> None:
    with pytest.raises(UnknownSkill):
        execute("does.not.exist", {}, job_ctx)


def test_bad_params_rejected_before_run(skill_registry, job_ctx) -> None:
    _register_echo()
    with pytest.raises(InvalidParams):
        execute("test.echo", {"text": ""}, job_ctx)  # min_length=1 violated
    # Nothing ran, nothing recorded.
    assert steps_repo.list_steps(job_ctx.conn, job_ctx.job_id) == []


def test_missing_permission_rejected(skill_registry, job_ctx) -> None:
    _register_echo("test.admin", permissions=("x.admin",))
    with pytest.raises(PermissionDenied):
        execute("test.admin", {"text": "hi"}, job_ctx)
    assert steps_repo.list_steps(job_ctx.conn, job_ctx.job_id) == []


def test_external_effect_returns_pending_confirmation(skill_registry, job_ctx) -> None:
    _register_echo("test.ext", effect="external")
    result = execute("test.ext", {"text": "hi"}, job_ctx)
    assert result.pending_confirmation
    assert result.value is None
    assert result.params == _EchoParams(text="hi")
    # Nothing executed yet, so no step recorded.
    assert steps_repo.list_steps(job_ctx.conn, job_ctx.job_id) == []
