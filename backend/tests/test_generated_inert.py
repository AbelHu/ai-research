"""Tests: feature jobs write **inert** generated code (implementation-plan T6.9)."""

from __future__ import annotations

import json

import pytest

import app.skills  # noqa: F401  -- ensure the live catalog is populated
from app.skills.codegen import (
    STATUS_INERT,
    get_bundle,
    is_inert,
    write_generated_skill,
)
from app.skills.registry import catalog

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


def test_generated_code_written_to_disk(tmp_path, skill_registry) -> None:
    path = write_generated_skill(tmp_path, JOB_CODE, "echo.py", GENERATED_CODE)

    assert path.is_file()
    assert path.parent.name == JOB_CODE
    assert "generated.echo" in path.read_text(encoding="utf-8")


def test_generated_bundle_is_inert(tmp_path, skill_registry) -> None:
    write_generated_skill(tmp_path, JOB_CODE, "echo.py", GENERATED_CODE)

    assert is_inert(tmp_path, JOB_CODE) is True
    bundle = get_bundle(tmp_path, JOB_CODE)
    assert bundle.status == STATUS_INERT
    assert bundle.files == ["echo.py"]

    manifest = json.loads((tmp_path / JOB_CODE / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["status"] == STATUS_INERT


def test_generated_skill_absent_from_live_catalog(tmp_path, skill_registry) -> None:
    write_generated_skill(tmp_path, JOB_CODE, "echo.py", GENERATED_CODE)

    # The file is on disk but never imported → the @skill never ran → not listed.
    names = {entry["name"] for entry in catalog()}
    assert "generated.echo" not in names


def test_invalid_filename_rejected(tmp_path, skill_registry) -> None:
    with pytest.raises(ValueError):
        write_generated_skill(tmp_path, JOB_CODE, "../escape.py", GENERATED_CODE)
    with pytest.raises(ValueError):
        write_generated_skill(tmp_path, JOB_CODE, "notpython.txt", GENERATED_CODE)


def test_multiple_files_tracked_in_manifest(tmp_path, skill_registry) -> None:
    write_generated_skill(tmp_path, JOB_CODE, "a.py", "# a\n")
    write_generated_skill(tmp_path, JOB_CODE, "b.py", "# b\n")
    bundle = get_bundle(tmp_path, JOB_CODE)
    assert sorted(bundle.files) == ["a.py", "b.py"]
