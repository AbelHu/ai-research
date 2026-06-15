"""Tests for the Boss deterministic router (implementation-plan T4.2)."""

from __future__ import annotations

import pytest

from app.roles.boss import BossDecision, UnroutableMessage, decide
from app.roles.envelope import Action, Role, RoleMessage


def _msg(action: Action, *, payload: dict | None = None, from_role: Role = Role.pm) -> RoleMessage:
    return RoleMessage(
        request_id=1,
        from_role=from_role,
        to_role=Role.boss,
        action=action,
        payload=payload or {},
    )


def test_route_request_goes_to_analyzer() -> None:
    decision = decide(_msg(Action.route_request))
    assert decision == BossDecision(Role.analyzer, Action.analyze)


def test_analysis_done_plan_ready_schedules_review_plan() -> None:
    decision = decide(_msg(Action.analysis_done, payload={"verdict": "plan_ready"}))
    assert decision == BossDecision(Role.company_expert, Action.review_plan)


def test_analysis_done_answer_ask_goes_to_junior() -> None:
    decision = decide(_msg(Action.analysis_done, payload={"verdict": "answer_ask"}))
    assert decision == BossDecision(Role.junior, Action.answer_ask)


def test_analysis_done_clarify_goes_to_pm() -> None:
    decision = decide(_msg(Action.analysis_done, payload={"verdict": "ask_clarify"}))
    assert decision == BossDecision(Role.pm, Action.clarify)


def test_analysis_done_append_rejected_goes_to_pm() -> None:
    decision = decide(_msg(Action.analysis_done, payload={"verdict": "append_rejected"}))
    assert decision == BossDecision(Role.pm, Action.undo_append)


def test_ask_done_delivers_to_pm() -> None:
    decision = decide(_msg(Action.ask_done, from_role=Role.junior))
    assert decision == BossDecision(Role.pm, Action.deliver)


def test_unknown_verdict_raises() -> None:
    with pytest.raises(UnroutableMessage):
        decide(_msg(Action.analysis_done, payload={"verdict": "made_up"}))


def test_unhandled_action_raises() -> None:
    with pytest.raises(UnroutableMessage):
        decide(_msg(Action.deliver, from_role=Role.boss))
