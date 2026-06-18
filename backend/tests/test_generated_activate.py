"""Tests: generated-code review → confirm → activate (implementation-plan T6.10)."""

from __future__ import annotations

import json

import pytest

import app.skills  # noqa: F401  -- ensure the live catalog is populated
from app.skills import codegen
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


# --- startup re-loading of confirmed skills (P0.2) ---------------------------


def _skill_module(name: str) -> str:
    """A minimal generated skill module that registers ``name`` when imported."""
    return (
        "from pydantic import BaseModel\n"
        "from app.skills.registry import skill\n\n"
        "class _P(BaseModel):\n"
        "    text: str\n\n"
        "class _R(BaseModel):\n"
        "    echoed: str\n\n"
        f"@skill(name={name!r}, description='echo', params=_P, returns=_R, "
        "permissions=[], effect='read')\n"
        "def _fn(params, ctx):\n"
        "    return _R(echoed=params.text)\n"
    )


def _mark_active(root, code: str) -> None:
    manifest = root / code / "manifest.json"
    data = json.loads(manifest.read_text())
    data["status"] = "active"
    manifest.write_text(json.dumps(data))


def test_load_active_reregisters_active_bundle(tmp_path, skill_registry) -> None:
    # Simulate a prior process having confirmed the bundle (manifest active) but
    # this fresh process not having imported it yet.
    code = "20260617090000"
    write_generated_skill(tmp_path, code, "m.py", _skill_module("generated.la_echo"))
    _mark_active(tmp_path, code)

    assert get_skill("generated.la_echo") is None  # not loaded in this process yet
    newly = codegen.load_active(tmp_path)
    assert "generated.la_echo" in newly
    assert get_skill("generated.la_echo") is not None  # survived the "restart"


def test_load_active_skips_inert_bundles(tmp_path, skill_registry) -> None:
    code = "20260617091000"
    write_generated_skill(tmp_path, code, "m.py", _skill_module("generated.inert_echo"))
    # Left inert (the default) → load_active must not import or register it.
    assert codegen.load_active(tmp_path) == []
    assert get_skill("generated.inert_echo") is None


def test_load_active_is_idempotent(tmp_path, skill_registry) -> None:
    code = "20260617092000"
    write_generated_skill(tmp_path, code, "m.py", _skill_module("generated.idem_echo"))
    _mark_active(tmp_path, code)

    first = codegen.load_active(tmp_path)
    second = codegen.load_active(tmp_path)
    assert "generated.idem_echo" in first
    assert second == []  # already loaded in this process → no-op


# --- confirm CLI (P0.1) -----------------------------------------------------


def test_confirm_cli_activates_bundle(tmp_path, skill_registry, monkeypatch, capsys) -> None:
    from app.cli import confirm as confirm_cli

    monkeypatch.setattr(codegen, "GENERATED_ROOT", tmp_path)
    code = "20260617093000"
    write_generated_skill(tmp_path, code, "m.py", _skill_module("generated.cli_echo"))

    rc = confirm_cli.main([code])
    assert rc == 0
    assert get_skill("generated.cli_echo") is not None
    assert codegen.is_inert(tmp_path, code) is False
    assert "generated.cli_echo" in capsys.readouterr().out


def test_confirm_cli_lists_bundles(tmp_path, skill_registry, monkeypatch, capsys) -> None:
    from app.cli import confirm as confirm_cli

    monkeypatch.setattr(codegen, "GENERATED_ROOT", tmp_path)
    code = "20260617094000"
    write_generated_skill(tmp_path, code, "m.py", _skill_module("generated.list_echo"))

    rc = confirm_cli.main(["--list"])
    assert rc == 0
    out = capsys.readouterr().out
    assert code in out and "inert" in out


def test_confirm_cli_decline_keeps_inert(tmp_path, skill_registry, monkeypatch, capsys) -> None:
    from app.cli import confirm as confirm_cli

    monkeypatch.setattr(codegen, "GENERATED_ROOT", tmp_path)
    code = "20260617095000"
    write_generated_skill(tmp_path, code, "m.py", _skill_module("generated.declined_echo"))

    rc = confirm_cli.main([code, "--decline"])
    assert rc == 0
    assert codegen.is_inert(tmp_path, code) is True
    assert get_skill("generated.declined_echo") is None


# --- multi-file bundles (P2) ------------------------------------------------


_BUNDLE_SKILL = '''from pydantic import BaseModel
from app.skills.registry import skill


class Params(BaseModel):
    text: str


class Result(BaseModel):
    echoed: str


@skill(
    name="generated.bundle_double",
    description="echo the input",
    params=Params,
    returns=Result,
    permissions=[],
    effect="read",
)
def run(params, ctx):
    return Result(echoed=params.text)
'''

_BUNDLE_TEST = '''from dbl import Params, run


def test_run():
    assert run(Params(text="x"), None).echoed == "x"
'''


def test_write_generated_bundle_records_files_and_tests(tmp_path) -> None:
    code = "20260618100000"
    test_code = "from m import add\n\n\ndef test_add():\n    assert add(1, 1) == 2\n"
    written = codegen.write_generated_bundle(
        tmp_path,
        code,
        files=[("m.py", "def add(a, b):\n    return a + b\n")],
        test_files=[("test_m.py", test_code)],
    )
    assert [p.name for p in written] == ["m.py"]

    bundle = codegen.get_bundle(tmp_path, code)
    assert bundle.files == ["m.py"]  # skill modules
    assert bundle.test_files == ["test_m.py"]  # validation-only
    assert bundle.status == "inert"
    assert (tmp_path / code / "test_m.py").exists()


def test_bundle_activation_imports_only_skill_modules(tmp_path, skill_registry) -> None:
    code = "20260618101000"
    codegen.write_generated_bundle(
        tmp_path,
        code,
        files=[("sk.py", _skill_module("generated.bundle_one"))],
        test_files=[("test_sk.py", "def test_ok():\n    assert True\n")],
    )
    # Only the skill module is imported on activation; the test file is not.
    activated = codegen.confirm_and_activate(tmp_path, code, confirmed=True)
    assert activated == ["generated.bundle_one"]
    assert get_skill("generated.bundle_one") is not None


def test_generated_bundle_passes_sandbox_validation(tmp_path) -> None:
    from app.coder.sandbox import RlimitSandbox, run_checks

    code = "20260618102000"
    codegen.write_generated_bundle(
        tmp_path,
        code,
        files=[("dbl.py", _BUNDLE_SKILL)],
        test_files=[("test_dbl.py", _BUNDLE_TEST)],
    )
    report = run_checks(tmp_path / code, sandbox=RlimitSandbox(), timeout=60)
    assert report.ok, report.summary
    assert report.by_name("import").ok
    assert report.by_name("lint").ok
    assert report.by_name("tests").ok
