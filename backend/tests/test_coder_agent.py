"""Tests for the agentic Coder loop (P3): generate → validate → repair → promote.

Most tests inject a fake validation seam (``check_bundle``) so they run fast and
deterministically without spawning subprocesses; one end-to-end test drives the
real sandbox (``RlimitSandbox``) on a known-good bundle.
"""

from __future__ import annotations

import json

import pytest

from app.advisor.wrapper import Advisor
from app.coder.agent import run_coder
from app.coder.sandbox import CheckResult, ValidationReport
from app.skills import codegen
from app.storage.db import connect
from app.storage.migrations import migrate
from app.storage.repos import requests as requests_repo
from tests.fakes import FakeProvider


def _bundle_json(
    *,
    skill: str = "dbl.py",
    test: str = "test_dbl.py",
    skill_code: str = "VALUE = 1\n",
    test_code: str = "def test_ok():\n    assert True\n",
    rationale: str = "ok",
) -> str:
    return json.dumps(
        {
            "files": [{"filename": skill, "code": skill_code}],
            "test_files": [{"filename": test, "code": test_code}],
            "rationale": rationale,
        }
    )


def _report(ok: bool) -> ValidationReport:
    check = CheckResult(
        name="import",
        ok=ok,
        skipped=False,
        timed_out=False,
        output="" if ok else "ImportError: boom",
        duration_ms=1,
    )
    return ValidationReport(ok=ok, checks=(check,))


@pytest.fixture
def conn():
    c = connect()
    migrate(c)
    try:
        yield c
    finally:
        c.close()


def _req(conn) -> int:
    return requests_repo.create_request(conn, title="feature: double a number").id


def _advisor(conn, responses: list[str]) -> Advisor:
    provider = FakeProvider(responses)
    return Advisor(resolve_provider=lambda _role: provider, conn=conn)


def test_run_coder_promotes_on_first_pass(conn, tmp_path) -> None:
    advisor = _advisor(conn, [_bundle_json()])
    outcome = run_coder(
        advisor,
        job_code="C1",
        goal="double",
        request_id=_req(conn),
        generated_root=tmp_path,
        check_bundle=lambda _d: _report(True),
    )
    assert outcome.ok and outcome.iterations == 1
    assert outcome.skill_modules == ["dbl.py"]
    bundle = codegen.get_bundle(tmp_path, "C1")
    assert bundle is not None and bundle.status == "inert"
    assert bundle.files == ["dbl.py"] and bundle.test_files == ["test_dbl.py"]


def test_run_coder_repairs_then_passes(conn, tmp_path) -> None:
    # generate (will fail) → repair (will pass).
    advisor = _advisor(conn, [_bundle_json(rationale="v1"), _bundle_json(rationale="v2")])
    reports = iter([_report(False), _report(True)])
    outcome = run_coder(
        advisor,
        job_code="C2",
        goal="double",
        request_id=_req(conn),
        generated_root=tmp_path,
        check_bundle=lambda _d: next(reports),
        max_iterations=2,
    )
    assert outcome.ok and outcome.iterations == 2
    assert outcome.rationale == "v2"  # the repaired bundle was promoted
    assert codegen.get_bundle(tmp_path, "C2") is not None


def test_run_coder_exhausts_budget_without_promotion(conn, tmp_path) -> None:
    advisor = _advisor(conn, [_bundle_json(), _bundle_json()])
    outcome = run_coder(
        advisor,
        job_code="C3",
        goal="double",
        request_id=_req(conn),
        generated_root=tmp_path,
        check_bundle=lambda _d: _report(False),
        max_iterations=2,
    )
    assert not outcome.ok and outcome.iterations == 2
    assert outcome.error is not None
    assert codegen.get_bundle(tmp_path, "C3") is None  # nothing promoted


def test_run_coder_without_validation_writes_inert(conn, tmp_path) -> None:
    advisor = _advisor(conn, [_bundle_json()])

    def _must_not_validate(_d):
        raise AssertionError("validation should be skipped")

    outcome = run_coder(
        advisor,
        job_code="C4",
        goal="double",
        request_id=_req(conn),
        generated_root=tmp_path,
        validate=False,
        check_bundle=_must_not_validate,
    )
    assert outcome.ok and outcome.iterations == 1
    assert codegen.get_bundle(tmp_path, "C4").status == "inert"


# --- end-to-end with the real sandbox ---------------------------------------

_SKILL = '''from pydantic import BaseModel
from app.skills.registry import skill


class Params(BaseModel):
    text: str


class Result(BaseModel):
    echoed: str


@skill(
    name="generated.agent_echo",
    description="echo the input",
    params=Params,
    returns=Result,
    permissions=[],
    effect="read",
)
def run(params, ctx):
    return Result(echoed=params.text)
'''

_TEST = '''from dbl import Params, run


def test_run():
    assert run(Params(text="hi"), None).echoed == "hi"
'''


def test_run_coder_real_sandbox_end_to_end(conn, tmp_path) -> None:
    from app.coder.sandbox import RlimitSandbox

    advisor = _advisor(conn, [_bundle_json(skill_code=_SKILL, test_code=_TEST)])
    outcome = run_coder(
        advisor,
        job_code="C5",
        goal="echo the input",
        request_id=_req(conn),
        generated_root=tmp_path,
        sandbox=RlimitSandbox(),
        timeout=60,
    )
    assert outcome.ok, (outcome.error, outcome.report.summary if outcome.report else None)
    assert outcome.report.by_name("tests").ok
    assert codegen.get_bundle(tmp_path, "C5").files == ["dbl.py"]
