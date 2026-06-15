"""Tests: generated-code review → confirm → activate (implementation-plan T6.10)."""

from __future__ import annotations

import pytest

import app.skills  # noqa: F401  -- ensure the live catalog is populated
from app.skills.codegen import (
    confirm_and_activate,
    is_inert,
    write_generated_skill,
)
from app.skills.context import SkillContext
from app.skills.registry import catalog, get_skill
from app.skills.runtime import execute
from app.storage.db import connect
from app.storage.migrations import migrate

GENERATED_CODE = """
from pydantic import BaseModel

from app.skills.registry import skill


class EchoParams(BaseModel):
    text: str


class EchoResult(BaseModel):
    echoed: str


@skill(
    name="generated.echo",
    description="echo back the input",
    params=EchoParams,
    returns=EchoResult,
    permissions=[],
    effect="read",
)
def generated_echo(params, ctx):
    return EchoResult(echoed=params.text)
"""

JOB_CODE = "20260615120000"


@pytest.fixture
def conn():
    c = connect()
    migrate(c)
    try:
        yield c
    finally:
        c.close()


def test_confirm_activates_and_runs_through_runtime(tmp_path, skill_registry, conn) -> None:
    write_generated_skill(tmp_path, JOB_CODE, "echo.py", GENERATED_CODE)
    assert "generated.echo" not in {e["name"] for e in catalog()}  # inert first

    activated = confirm_and_activate(tmp_path, JOB_CODE, confirmed=True)

    assert activated == ["generated.echo"]
    assert "generated.echo" in {e["name"] for e in catalog()}  # now registered
    assert is_inert(tmp_path, JOB_CODE) is False
    assert get_skill("generated.echo") is not None

    # And it is runnable through the deterministic skill runtime.
    ctx = SkillContext(user_id=0, conn=conn, permissions=frozenset(), job_id=None)
    result = execute("generated.echo", {"text": "hello"}, ctx)
    assert result.ok
    assert result.value.echoed == "hello"


def test_decline_leaves_it_inert(tmp_path, skill_registry) -> None:
    write_generated_skill(tmp_path, JOB_CODE, "echo.py", GENERATED_CODE)

    activated = confirm_and_activate(tmp_path, JOB_CODE, confirmed=False)

    assert activated == []
    assert is_inert(tmp_path, JOB_CODE) is True
    assert "generated.echo" not in {e["name"] for e in catalog()}
    assert get_skill("generated.echo") is None


def test_confirm_generated_code_policy_default_on() -> None:
    from app.config.policies import Policies

    assert Policies().confirm_generated_code is True
