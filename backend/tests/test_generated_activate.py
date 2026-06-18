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
