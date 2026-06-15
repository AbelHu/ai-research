"""The Senior Worker — task execution (design-spec §6A, §6B, §8.4; plan T6.5).

Runs a phase's tasks **respecting dependencies** (a task waits until all the
tasks it ``depends_on`` are ``Resolved``) and each task's ``run_mode``. For each
task it asks the advisor for a validated `ProposedAction`, runs it through the
**skill runtime** (validate → policy gate → execute → record a `steps` row), and
drives the task ``Approved → InProgress → Resolved`` via the lifecycle setters.

Execution is dependency-ordered and deterministic; the AI only proposes the
action, code runs it (AI stays out of the control path).
"""

from __future__ import annotations

import json
from dataclasses import dataclass

import app.skills  # noqa: F401  -- ensure @skill registration
from app.advisor.wrapper import Advisor
from app.roles.envelope import Role
from app.skills import runtime
from app.skills.context import SkillContext
from app.skills.registry import catalog
from app.storage.repos import plans as plans_repo
from app.storage.repos.plans import Phase, PlanTask

_ACTOR = Role.senior_worker

# Read-leaning default grant for a worker (the policy gate still enforces it).
DEFAULT_PERMISSIONS = frozenset({"memory.read", "memory.write", "library.read"})


class DependencyCycle(RuntimeError):
    """Raised when a phase's tasks can't be ordered (a dependency cycle)."""


@dataclass(frozen=True)
class TaskRun:
    task_id: int
    skill: str
    step_id: int | None
    status: str  # the task's terminal status after the run


def _topological_order(tasks: list[PlanTask]) -> list[PlanTask]:
    """Return tasks in dependency order (deps before dependents), id-stable.

    Independent tasks keep ascending-id order so the run is deterministic; a
    cycle (or a dangling dependency) raises rather than silently dropping work.
    """
    by_id = {t.id: t for t in tasks}
    done: set[int] = set()
    order: list[PlanTask] = []
    remaining = sorted(tasks, key=lambda t: t.id)
    while remaining:
        progressed = False
        still: list[PlanTask] = []
        for task in remaining:
            deps = [d for d in task.depends_on if d in by_id]
            if all(d in done for d in deps):
                order.append(task)
                done.add(task.id)
                progressed = True
            else:
                still.append(task)
        if not progressed:
            raise DependencyCycle(
                f"unresolvable task dependencies in phase: {[t.id for t in remaining]}"
            )
        remaining = still
    return order


def run_task(
    conn,
    advisor: Advisor,
    task: PlanTask,
    *,
    request_id: int,
    job_id: int,
    user_id: int | None = None,
    permissions: frozenset[str] = DEFAULT_PERMISSIONS,
) -> TaskRun:
    """Execute one task: propose → run a skill → record → ``Resolved`` (§8.4)."""
    plans_repo.set_task_status(conn, task.id, "InProgress", actor=_ACTOR)

    action = advisor.next_action(
        goal=task.title or "",
        catalog=json.dumps(catalog(), ensure_ascii=False),
        request_id=request_id,
        job_id=job_id,
    )
    ctx = SkillContext(
        user_id=user_id if user_id is not None else 0,
        conn=conn,
        permissions=permissions,
        job_id=job_id,
        task_id=task.id,
    )
    result = runtime.execute(action.skill, action.params, ctx)

    plans_repo.set_task_status(conn, task.id, "Resolved", actor=_ACTOR)
    return TaskRun(task_id=task.id, skill=action.skill, step_id=result.step_id, status="Resolved")


def run_phase(
    conn,
    advisor: Advisor,
    phase: Phase,
    *,
    request_id: int,
    job_id: int,
    user_id: int | None = None,
    permissions: frozenset[str] = DEFAULT_PERMISSIONS,
) -> list[TaskRun]:
    """Run all of a phase's tasks in dependency order; return the runs in order.

    Moves the phase ``Active -> InProgress`` on the first task. Phase resolution
    (all tasks done → ``Resolved``) is the Plan Expert's job (T6.6).
    """
    current_phase = plans_repo.get_phase(conn, phase.id)
    if current_phase is not None and current_phase.status == "Active":
        plans_repo.set_phase_status(conn, phase.id, "InProgress", actor=_ACTOR)

    tasks = plans_repo.list_tasks(conn, phase.id)
    runs: list[TaskRun] = []
    for task in _topological_order(tasks):
        current = plans_repo.get_task(conn, task.id)
        if current is None or current.status != "Approved":
            continue  # already run / not approved → skip
        runs.append(
            run_task(
                conn,
                advisor,
                current,
                request_id=request_id,
                job_id=job_id,
                user_id=user_id,
                permissions=permissions,
            )
        )
    return runs
