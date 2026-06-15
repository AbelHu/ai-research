"""The Boss — deterministic router (design-spec §6B, §6D; implementation-plan T4.2).

The Boss is **flow + scheduling**, never AI. It reads an inbound envelope's
`action` (and, for `analysis_done`, the Analyzer's **verdict**) and returns the
next routing decision — which role to schedule with which verb. The model never
sets `action`; code maps a validated verdict to the next verb, keeping AI out of
the control path.

This P4 skeleton covers the **simple-ask** flow plus the verdict fan-out the
Analyzer produces; the per-job-runner verbs (`run_phase`/`run_task`/…) land in P6.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.roles.envelope import Action, Role, RoleMessage

# The Analyzer's verdict (carried on `analysis_done.payload["verdict"]`) → the
# next (role, verb) the Boss schedules. Deterministic, exhaustive table (§6D).
_VERDICT_ROUTES: dict[str, tuple[Role, Action]] = {
    "answer_ask": (Role.junior, Action.answer_ask),
    "plan_ready": (Role.company_expert, Action.review_plan),
    "ask_clarify": (Role.pm, Action.clarify),
    "append_rejected": (Role.pm, Action.undo_append),
}


class UnroutableMessage(RuntimeError):
    """Raised when the Boss has no deterministic route for an envelope.

    A silent drop would strand the request; surfacing it keeps the control path
    total — every routed envelope is either scheduled or loudly rejected.
    """

    def __init__(self, msg: RoleMessage, detail: str) -> None:
        self.msg = msg
        super().__init__(f"no route for action={msg.action.value!r}: {detail}")


@dataclass(frozen=True)
class BossDecision:
    """The next hand-off the Boss schedules."""

    to_role: Role
    action: Action


def decide(msg: RoleMessage) -> BossDecision:
    """Return the next routing decision for an inbound envelope (§6D)."""
    if msg.action is Action.route_request:
        # A new/appended request → authoritative Analyzer validation + classify.
        return BossDecision(Role.analyzer, Action.analyze)

    if msg.action is Action.analysis_done:
        verdict = msg.payload.get("verdict")
        route = _VERDICT_ROUTES.get(verdict) if isinstance(verdict, str) else None
        if route is None:
            raise UnroutableMessage(msg, f"unknown analysis verdict {verdict!r}")
        return BossDecision(*route)

    if msg.action is Action.ask_done:
        # Validated answer ready → PM delivers it to the user.
        return BossDecision(Role.pm, Action.deliver)

    raise UnroutableMessage(msg, "no rule for this action in the P4 skeleton")
