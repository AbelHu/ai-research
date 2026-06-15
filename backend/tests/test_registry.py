"""Tests for the skill registry + catalog (implementation-plan T2.1)."""

from __future__ import annotations

import pytest
from pydantic import BaseModel

from app.skills.registry import catalog, get_skill, skill


class _DummyParams(BaseModel):
    text: str


class _DummyResult(BaseModel):
    ok: bool


def _register(name: str, *, effect: str):
    @skill(
        name=name,
        description="dummy",
        params=_DummyParams,
        returns=_DummyResult,
        permissions=["x.read"],
        effect=effect,
    )
    def _fn(params: _DummyParams, ctx) -> _DummyResult:  # pragma: no cover - not run here
        return _DummyResult(ok=True)

    return _fn


def test_register_and_catalog_entry(skill_registry) -> None:
    _register("test.dummy", effect="read")

    spec = get_skill("test.dummy")
    assert spec is not None
    assert spec.permissions == ("x.read",)

    entry = next(e for e in catalog() if e["name"] == "test.dummy")
    assert entry["description"] == "dummy"
    assert entry["side_effects"] is False
    # JSON Schema is derived from the pydantic params model.
    assert entry["params_schema"]["properties"]["text"]["type"] == "string"


def test_duplicate_skill_rejected(skill_registry) -> None:
    _register("test.dup", effect="read")
    with pytest.raises(ValueError, match="duplicate skill"):
        _register("test.dup", effect="read")


def test_side_effects_derived_from_effect_class(skill_registry) -> None:
    _register("test.read", effect="read")
    _register("test.local", effect="local_write")
    _register("test.ext", effect="external")

    flags = {e["name"]: e["side_effects"] for e in catalog()}
    assert flags["test.read"] is False
    assert flags["test.local"] is True
    assert flags["test.ext"] is True


def test_catalog_allowed_filter(skill_registry) -> None:
    _register("test.a", effect="read")
    _register("test.b", effect="read")
    names = {e["name"] for e in catalog(allowed={"test.a"})}
    assert names == {"test.a"}
