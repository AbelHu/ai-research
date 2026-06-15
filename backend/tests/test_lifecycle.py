"""Tests for the status lifecycle state machine (implementation-plan T6.2)."""

from __future__ import annotations

import pytest

from app.roles.envelope import Role
from app.roles.lifecycle import (
    TERMINAL,
    Entity,
    IllegalTransition,
    Status,
    can_transition,
    validate_transition,
)

# --- legal transitions pass -------------------------------------------------


@pytest.mark.parametrize(
    "entity,frm,to,actor",
    [
        (Entity.plan, Status.New, Status.Approved, Role.company_expert),
        (Entity.plan, Status.Approved, Status.InProgress, Role.boss),
        (Entity.plan, Status.InProgress, Status.Resolved, Role.company_expert),
        (Entity.plan, Status.Resolved, Status.Closed, Role.librarian),
        (Entity.phase, Status.Approved, Status.Active, Role.boss),
        (Entity.phase, Status.Active, Status.InProgress, Role.senior_worker),
        (Entity.phase, Status.InProgress, Status.Resolved, Role.plan_expert),
        (Entity.phase, Status.Resolved, Status.Closed, Role.company_expert),
        (Entity.phase, Status.Resolved, Status.Active, Role.company_expert),  # decline
        (Entity.task, Status.Approved, Status.InProgress, Role.senior_worker),
        (Entity.task, Status.InProgress, Status.Resolved, Role.senior_worker),
        (Entity.task, Status.Resolved, Status.Closed, Role.company_expert),
        (Entity.task, Status.Resolved, Status.InProgress, Role.boss),  # reactivate
    ],
)
def test_legal_transitions(entity, frm, to, actor) -> None:
    assert can_transition(entity, frm, to, actor)
    validate_transition(entity, frm, to, actor)  # no raise


# --- illegal: wrong actor ---------------------------------------------------


def test_wrong_actor_rejected() -> None:
    # Only the Company Expert closes a phase; the Senior Worker may not.
    assert not can_transition(Entity.phase, Status.Resolved, Status.Closed, Role.senior_worker)
    with pytest.raises(IllegalTransition):
        validate_transition(Entity.phase, Status.Resolved, Status.Closed, Role.senior_worker)


def test_plan_close_is_librarian_only() -> None:
    assert can_transition(Entity.plan, Status.Resolved, Status.Closed, Role.librarian)
    assert not can_transition(Entity.plan, Status.Resolved, Status.Closed, Role.company_expert)


# --- illegal: bad edge ------------------------------------------------------


def test_skipping_states_rejected() -> None:
    # New cannot jump straight to Resolved.
    with pytest.raises(IllegalTransition):
        validate_transition(Entity.task, Status.New, Status.Resolved, Role.senior_worker)


def test_no_transition_out_of_terminal() -> None:
    for terminal in TERMINAL:
        assert not can_transition(Entity.plan, terminal, Status.New, Role.boss)
        assert not can_transition(Entity.task, terminal, Status.Abandoned, Role.user)


# --- abandon ----------------------------------------------------------------


def test_abandon_from_active_states() -> None:
    for entity in (Entity.plan, Entity.phase, Entity.task):
        assert can_transition(entity, Status.InProgress, Status.Abandoned, Role.user)
        assert can_transition(entity, Status.New, Status.Abandoned, Role.company_expert)


def test_abandon_actor_restricted() -> None:
    # A Senior Worker can't unilaterally abandon a task.
    assert not can_transition(Entity.task, Status.InProgress, Status.Abandoned, Role.senior_worker)
