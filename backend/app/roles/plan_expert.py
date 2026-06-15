"""The Plan Expert — phase resolution + final report (design-spec §6B; plan T6.6).

Runs inside the per-job runner. When **all** of a phase's tasks are ``Resolved``
it writes the **phase report** and moves the phase ``InProgress -> Resolved``
(then the Company Expert signs it off, T6.3). When all phases are ``Closed`` it
**assembles the plan's final report** (§9.2) and submits it for sign-off /
delivery. Deterministic assembly here; the gain/AI enrichment can layer on later
without changing this control path.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.memory.reports import FinalReport, Gain
from app.roles.envelope import Role
from app.storage.repos import plans as plans_repo
from app.storage.repos import requests as requests_repo
from app.storage.repos.plans import Phase, Plan

_ACTOR = Role.plan_expert


@dataclass(frozen=True)
class PhaseResolution:
    phase_id: int
    resolved: bool
    report_ref: str | None


def resolve_phase(conn, phase: Phase) -> PhaseResolution:
    """Resolve a phase once all its tasks are done; write the phase report (§6B).

    Returns ``resolved=False`` (a no-op) when the phase has no tasks or any task
    is not yet ``Resolved``/``Closed`` — so the runner can call it idempotently.
    """
    tasks = plans_repo.list_tasks(conn, phase.id)
    if not tasks or not all(t.status in ("Resolved", "Closed") for t in tasks):
        return PhaseResolution(phase_id=phase.id, resolved=False, report_ref=None)

    current = plans_repo.get_phase(conn, phase.id)
    if current is None or current.status != "InProgress":
        return PhaseResolution(
            phase_id=phase.id, resolved=False, report_ref=current.report_ref if current else None
        )

    report_ref = f"phase-{phase.id}-report"
    plans_repo.set_phase_report_ref(conn, phase.id, report_ref)
    plans_repo.set_phase_status(conn, phase.id, "Resolved", actor=_ACTOR)
    return PhaseResolution(phase_id=phase.id, resolved=True, report_ref=report_ref)


def all_phases_closed(conn, plan: Plan) -> bool:
    """Whether every phase of a plan is ``Closed`` (ready for the final report)."""
    phases = plans_repo.list_phases(conn, plan.id)
    return bool(phases) and all(p.status == "Closed" for p in phases)


def assemble_final_report(conn, plan: Plan, *, gain: Gain | None = None) -> FinalReport:
    """Build the plan's §9.2 final report from its phases (Plan Expert, §6B).

    Deterministic assembly: the title/keywords come from the request + phases.
    The Librarian commits it (T5.8); the Company Expert / PM handle sign-off and
    user confirmation.
    """
    job = requests_repo.get_job(conn, plan.job_id)
    if job is None:
        raise ValueError(f"plan {plan.id} has no job")
    request = requests_repo.get_request(conn, job.request_id)
    if request is None:
        raise ValueError(f"job {job.id} has no request")

    phases = plans_repo.list_phases(conn, plan.id)
    phase_titles = [p.title or f"phase {p.idx}" for p in phases]
    brief = "Completed phases: " + ", ".join(phase_titles) if phase_titles else "No phases."

    return FinalReport(
        request_id=request.id,
        kind=job.kind,  # type: ignore[arg-type]  -- jobs.kind is constrained to the Kind literal
        title=request.title or "untitled request",
        keywords=[t.lower() for title in phase_titles for t in title.split()][:12],
        tags=[],
        brief_description=brief,
        outcome="delivered",
        gain=gain if gain is not None else Gain(),
    )
