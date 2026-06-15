"""Status lifecycle state machine (design-spec §6B; implementation-plan T6.2).

Pure, deterministic rules for the **plan / phase / task** status model and the
**who-sets-what** authority from §6B. No DB, no AI: callers validate a proposed
transition before writing it, so an illegal status change (or one attempted by
the wrong role) is rejected loudly rather than silently corrupting the flow.

States: ``New, Approved, Active, InProgress, Resolved, Closed`` + ``Abandoned``
(reachable from any non-terminal state). Terminal: ``Closed``, ``Abandoned``.
"""

from __future__ import annotations

from enum import Enum

from app.roles.envelope import Role


class Entity(str, Enum):
    plan = "plan"
    phase = "phase"
    task = "task"


class Status(str, Enum):
    New = "New"
    Approved = "Approved"
    Active = "Active"
    InProgress = "InProgress"
    Resolved = "Resolved"
    Closed = "Closed"
    Abandoned = "Abandoned"


# Terminal states have no outgoing transitions.
TERMINAL: frozenset[Status] = frozenset({Status.Closed, Status.Abandoned})


class IllegalTransition(ValueError):
    """Raised when a status change is not allowed (bad edge or wrong actor)."""

    def __init__(self, entity: Entity, frm: Status, to: Status, actor: Role | None) -> None:
        self.entity = entity
        self.frm = frm
        self.to = to
        self.actor = actor
        who = actor.value if actor is not None else "?"
        super().__init__(f"illegal {entity.value} transition {frm.value} -> {to.value} by {who!r}")


# (entity, from, to) -> the roles permitted to make that transition (§6B).
_TRANSITIONS: dict[tuple[Entity, Status, Status], frozenset[Role]] = {
    # --- Plan (1:1 with the job) ---
    (Entity.plan, Status.New, Status.Approved): frozenset({Role.company_expert}),
    (Entity.plan, Status.Approved, Status.InProgress): frozenset({Role.boss}),
    (Entity.plan, Status.InProgress, Status.Resolved): frozenset({Role.company_expert}),
    (Entity.plan, Status.Resolved, Status.Closed): frozenset({Role.librarian}),
    # --- Phase ---
    (Entity.phase, Status.New, Status.Approved): frozenset({Role.company_expert}),
    (Entity.phase, Status.Approved, Status.Active): frozenset({Role.boss}),
    (Entity.phase, Status.Active, Status.InProgress): frozenset({Role.senior_worker}),
    (Entity.phase, Status.InProgress, Status.Resolved): frozenset({Role.plan_expert}),
    (Entity.phase, Status.Resolved, Status.Closed): frozenset({Role.company_expert}),
    (Entity.phase, Status.Resolved, Status.Active): frozenset({Role.company_expert}),  # decline
    # --- Task ---
    (Entity.task, Status.New, Status.Approved): frozenset({Role.company_expert}),
    (Entity.task, Status.Approved, Status.InProgress): frozenset({Role.senior_worker}),
    (Entity.task, Status.InProgress, Status.Resolved): frozenset({Role.senior_worker}),
    (Entity.task, Status.Resolved, Status.Closed): frozenset({Role.company_expert}),
    (Entity.task, Status.Resolved, Status.InProgress): frozenset({Role.boss}),  # reactivate
}

# Any non-terminal state may be Abandoned by the user or the Company Expert.
_ABANDON_ACTORS: frozenset[Role] = frozenset({Role.user, Role.company_expert})


def allowed_actors(entity: Entity, frm: Status, to: Status) -> frozenset[Role]:
    """Roles permitted to drive ``frm -> to`` for ``entity`` (empty if illegal)."""
    if to is Status.Abandoned:
        return frozenset() if frm in TERMINAL else _ABANDON_ACTORS
    return _TRANSITIONS.get((entity, frm, to), frozenset())


def can_transition(entity: Entity, frm: Status, to: Status, actor: Role) -> bool:
    """Whether ``actor`` may drive ``entity`` from ``frm`` to ``to``."""
    return actor in allowed_actors(entity, frm, to)


def validate_transition(entity: Entity, frm: Status, to: Status, actor: Role) -> None:
    """Raise `IllegalTransition` unless the transition is allowed for ``actor``."""
    if not can_transition(entity, frm, to, actor):
        raise IllegalTransition(entity, frm, to, actor)
