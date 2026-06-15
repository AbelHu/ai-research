"""Tests for the Coder role — feature-job skill generation, gated (T6.9/T6.10).

Offline: the advisor returns a canned `GeneratedSkill`; the Coder writes it
**inert** under a tmp generated-root. Activation stays a separate, gated step, so
these assert the code lands on disk inert and is never imported/executed.
"""

from __future__ import annotations

import json

import pytest

from app.advisor.wrapper import Advisor
from app.roles import coder
from app.skills import codegen
from app.storage.db import connect
from app.storage.migrations import migrate
from app.storage.repos import requests as requests_repo
from tests.fakes import FakeProvider

GENERATED = json.dumps(
    {
        "skill_name": "generated.celsius_to_f",
        "module_filename": "celsius_to_f.py",
        "code": (
            "from pydantic import BaseModel\n"
            "from app.skills.registry import skill\n\n"
            "class P(BaseModel):\n    c: float\n\n"
            "class R(BaseModel):\n    f: float\n\n"
            '@skill(name="generated.celsius_to_f", description="c to f", '
            'params=P, returns=R, permissions=[], effect="read")\n'
            "def run(params, ctx):\n    return R(f=params.c * 9 / 5 + 32)\n"
        ),
        "rationale": "repeatable unit conversion",
    }
)


@pytest.fixture
def conn():
    c = connect()
    migrate(c)
    try:
        yield c
    finally:
        c.close()


def _advisor(conn, canned: str) -> Advisor:
    return Advisor(resolve_provider=lambda _role: FakeProvider(canned), conn=conn)


def _feature_job(conn):
    req = requests_repo.create_request(conn, title="convert temperatures")
    job = requests_repo.create_job(conn, request_id=req.id, kind="feature", complexity="complex")
    return req, job


def test_generate_writes_inert_skill(conn, tmp_path) -> None:
    req, job = _feature_job(conn)

    result = coder.generate_feature_skill(
        conn, _advisor(conn, GENERATED), job_id=job.id, goal="convert C to F", root=tmp_path
    )

    assert result.skill_name == "generated.celsius_to_f"
    assert result.filename == "celsius_to_f.py"
    assert result.activated is False
    # The code is on disk under the request code, recorded inert in the manifest.
    assert result.path.is_file()
    assert codegen.is_inert(tmp_path, req.code)
    bundle = codegen.get_bundle(tmp_path, req.code)
    assert bundle is not None and bundle.files == ["celsius_to_f.py"]


def test_generated_skill_is_not_registered_until_confirmed(conn, tmp_path, skill_registry) -> None:
    req, job = _feature_job(conn)
    coder.generate_feature_skill(
        conn, _advisor(conn, GENERATED), job_id=job.id, goal="convert C to F", root=tmp_path
    )
    # Writing inert must NOT register the skill (no import/exec happened).
    assert "generated.celsius_to_f" not in skill_registry

    # Only an explicit, confirmed activation registers it (the gated step).
    activated = codegen.confirm_and_activate(tmp_path, req.code, confirmed=True)
    assert "generated.celsius_to_f" in activated
    assert "generated.celsius_to_f" in skill_registry


def test_generate_rejects_bad_skill_name(conn, tmp_path) -> None:
    # A skill_name not under `generated.` is a schema violation → escalates.
    bad = json.dumps({"skill_name": "evil.shadow", "module_filename": "x.py", "code": "x = 1\n"})
    req, job = _feature_job(conn)
    with pytest.raises(Exception):  # noqa: B017 - AdvisorValidationError on escalate
        coder.generate_feature_skill(
            conn, _advisor(conn, bad), job_id=job.id, goal="g", root=tmp_path
        )
