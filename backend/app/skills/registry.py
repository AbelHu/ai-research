"""Skill registry: `SkillSpec` + the `@skill` decorator (design-spec §8.1-8.3).

A **skill** is a plain function plus a typed contract (name, params/returns
pydantic models, permissions, effect class). The `@skill` decorator binds the
function to a `SkillSpec` and records it in a process-wide ``REGISTRY``.

``catalog()`` emits the machine-readable view the AI is allowed to see — names,
descriptions and parameter **JSON Schemas** only — so the model can propose a
call it can't malform (and deterministic code still validates it, §8.6).
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal

from pydantic import BaseModel

if TYPE_CHECKING:
    from app.skills.context import SkillContext

# Effect class — generalizes the spec's ``side_effects: bool`` (implementation-plan T2.3):
#   read         -> no writes that leave the machine; never user-confirmed.
#   local_write  -> writes only to the local DB/disk; permission-gated, not confirmed.
#   external     -> leaves the machine / user-visible; requires confirmation (§9.1).
EffectClass = Literal["read", "local_write", "external"]

# A skill implementation: pure function of (validated params, deterministic ctx).
SkillFn = Callable[[Any, "SkillContext"], BaseModel]


@dataclass(frozen=True)
class SkillSpec:
    """The typed contract bound to a skill function."""

    name: str
    description: str
    params_model: type[BaseModel]
    returns_model: type[BaseModel]
    permissions: tuple[str, ...]
    effect: EffectClass
    fn: SkillFn

    @property
    def params_schema(self) -> dict[str, Any]:
        """JSON Schema for the params model — the only param shape the AI sees."""
        return self.params_model.model_json_schema()

    @property
    def side_effects(self) -> bool:
        """Spec §8.3 catalog flag: anything that isn't purely read-only."""
        return self.effect != "read"


# Process-wide name -> spec map, populated by ``@skill`` at import (§8.7).
REGISTRY: dict[str, SkillSpec] = {}


def register(spec: SkillSpec) -> None:
    """Add a spec to the registry, rejecting duplicate names."""
    if spec.name in REGISTRY:
        raise ValueError(f"duplicate skill: {spec.name!r}")
    REGISTRY[spec.name] = spec


def skill(
    *,
    name: str,
    description: str,
    params: type[BaseModel],
    returns: type[BaseModel],
    permissions: Sequence[str] = (),
    effect: EffectClass,
) -> Callable[[SkillFn], SkillFn]:
    """Decorator binding a function to a `SkillSpec` and registering it."""

    def wrap(fn: SkillFn) -> SkillFn:
        register(
            SkillSpec(
                name=name,
                description=description,
                params_model=params,
                returns_model=returns,
                permissions=tuple(permissions),
                effect=effect,
                fn=fn,
            )
        )
        return fn

    return wrap


def get_skill(name: str) -> SkillSpec | None:
    """Return the spec for ``name`` (or ``None`` if not registered)."""
    return REGISTRY.get(name)


def catalog(allowed: set[str] | None = None) -> list[dict[str, Any]]:
    """The machine-readable skill catalog the advisor is shown (§8.3).

    Args:
        allowed: if given, restrict the catalog to these skill names (the set a
            given agent/role is permitted to propose).
    """
    return [
        {
            "name": spec.name,
            "description": spec.description,
            "params_schema": spec.params_schema,
            "side_effects": spec.side_effects,
        }
        for spec in REGISTRY.values()
        if allowed is None or spec.name in allowed
    ]
