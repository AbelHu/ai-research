"""Tests for the skill policy gate (implementation-plan T2.3)."""

from __future__ import annotations

import pytest
from pydantic import BaseModel

from app.skills import policy
from app.skills.context import SkillContext
from app.skills.policy import PermissionDenied
from app.skills.registry import get_skill, skill
from app.storage.db import connect


class _P(BaseModel):
    pass


class _R(BaseModel):
    ok: bool = True


def _register(name: str, *, effect: str, permissions: list[str]):
    @skill(
        name=name,
        description="d",
        params=_P,
        returns=_R,
        permissions=permissions,
        effect=effect,
    )
    def _fn(params: _P, ctx) -> _R:  # pragma: no cover - not run here
        return _R()


def _ctx(granted: set[str]) -> SkillContext:
    return SkillContext(user_id=1, conn=connect(), permissions=frozenset(granted))


def test_read_and_local_write_skip_confirmation(skill_registry) -> None:
    _register("p.read", effect="read", permissions=["x.read"])
    _register("p.local", effect="local_write", permissions=["x.write"])
    ctx = _ctx({"x.read", "x.write"})

    assert policy.needs_confirmation(get_skill("p.read"), ctx) is False
    assert policy.needs_confirmation(get_skill("p.local"), ctx) is False


def test_external_requires_confirmation(skill_registry) -> None:
    _register("p.ext", effect="external", permissions=["x.net"])
    ctx = _ctx({"x.net"})
    assert policy.needs_confirmation(get_skill("p.ext"), ctx) is True


def test_check_passes_when_permissions_granted(skill_registry) -> None:
    _register("p.ok", effect="read", permissions=["x.read"])
    policy.check(get_skill("p.ok"), _ctx({"x.read"}))  # no raise


def test_missing_permission_rejected(skill_registry) -> None:
    _register("p.denied", effect="read", permissions=["x.read", "x.admin"])
    with pytest.raises(PermissionDenied) as exc:
        policy.check(get_skill("p.denied"), _ctx({"x.read"}))
    assert exc.value.missing == ["x.admin"]
