"""Policy gate: permission + effect-class confirmation rules (§8.6, T2.3).

Two independent checks sit between an AI suggestion and an actual call:

* **Permission gate** — every permission a skill declares must be present in the
  context's granted set, else the call is rejected before anything runs.
* **Confirmation gate** — derived from the skill's **effect class**: only
  ``external`` / user-visible effects require user confirmation. ``read`` and
  ``local_write`` (e.g. ``memory.write``, ``memory.tag``, reinforcement touches)
  are permission-gated but **not** user-confirmed, so we don't over-prompt on
  local DB writes (§9.1).
"""

from __future__ import annotations

from app.skills.context import SkillContext
from app.skills.registry import SkillSpec


class PermissionDenied(RuntimeError):
    """Raised when the context lacks a permission the skill requires."""

    def __init__(self, skill_name: str, missing: list[str]) -> None:
        self.skill_name = skill_name
        self.missing = missing
        super().__init__(f"skill {skill_name!r} requires missing permission(s): {missing}")


def check(spec: SkillSpec, ctx: SkillContext) -> None:
    """Raise `PermissionDenied` unless every required permission is granted."""
    missing = [p for p in spec.permissions if p not in ctx.permissions]
    if missing:
        raise PermissionDenied(spec.name, missing)


def needs_confirmation(spec: SkillSpec, ctx: SkillContext) -> bool:
    """Whether a skill must be user-confirmed before running.

    Only ``external`` effects prompt; ``read`` and ``local_write`` never do.
    ``ctx`` is accepted for future per-user/per-channel policy without changing
    the call sites.
    """
    return spec.effect == "external"
