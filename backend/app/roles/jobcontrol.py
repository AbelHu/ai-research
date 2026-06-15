"""Pause / resume / abandon for a per-job runner (design-spec §6B; plan T6.7).

Two coordinated mechanisms:

* **Pause** — ``jobs.paused`` (DB) is the **durable source of truth**; a per-job
  ``asyncio.Event`` is the **live signal**. ``pause()`` clears the event and sets
  the DB flag; the runner only observes it at a **checkpoint between atomic
  steps** (``await control.checkpoint()``) where it parks until ``resume()`` sets
  the event again. A paused job **holds its slot** (the named §6B tradeoff).

* **Abandon** — cancel the runner task (``task.cancel()`` → ``CancelledError``);
  the runner's ``except CancelledError`` marks the plan/phases/tasks
  ``Abandoned`` (via the lifecycle setters) and the scheduler frees the slot.

No status is mutated on pause — every entity keeps its current state, so resume
always knows where it was (§6B).
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable

from app.roles.envelope import Role
from app.roles.lifecycle import Status
from app.storage.repos import plans as plans_repo
from app.storage.repos import requests as requests_repo


class JobControl:
    """Live pause/resume signal for one job, backed by ``jobs.paused`` (DB)."""

    def __init__(self, conn, job_id: int) -> None:
        self.conn = conn
        self.job_id = job_id
        self._resume = asyncio.Event()
        self._resume.set()  # not paused initially

    @property
    def paused(self) -> bool:
        return not self._resume.is_set()

    def pause(self) -> None:
        """Set the durable flag + clear the live signal (the Boss pauses)."""
        requests_repo.set_job_paused(self.conn, self.job_id, True)
        self._resume.clear()

    def resume(self) -> None:
        """Clear the durable flag + set the live signal (the Boss resumes)."""
        requests_repo.set_job_paused(self.conn, self.job_id, False)
        self._resume.set()

    async def checkpoint(self) -> None:
        """Park here while paused (a step boundary); return at once when running."""
        await self._resume.wait()


def abandon_tree(conn, plan_id: int, *, actor: Role = Role.user) -> int:
    """Mark a plan + its phases + tasks ``Abandoned`` (non-terminal only).

    Returns the number of entities transitioned. Safe to call during a runner's
    cancellation handler — it only touches still-active rows.
    """
    changed = 0
    for phase in plans_repo.list_phases(conn, plan_id):
        for task in plans_repo.list_tasks(conn, phase.id):
            if Status(task.status) not in {Status.Closed, Status.Abandoned}:
                plans_repo.set_task_status(conn, task.id, "Abandoned", actor=actor)
                changed += 1
        refreshed = plans_repo.get_phase(conn, phase.id)
        if refreshed and Status(refreshed.status) not in {Status.Closed, Status.Abandoned}:
            plans_repo.set_phase_status(conn, phase.id, "Abandoned", actor=actor)
            changed += 1
    plan = plans_repo.get_plan(conn, plan_id)
    if plan and Status(plan.status) not in {Status.Closed, Status.Abandoned}:
        plans_repo.set_plan_status(conn, plan_id, "Abandoned", actor=actor)
        changed += 1
    return changed


async def run_job(
    control: JobControl,
    conn,
    plan_id: int,
    *,
    process_phase: Callable[[object], Awaitable[None]],
) -> None:
    """Drive a plan's phases with pause checkpoints + abandon-on-cancel (§6B).

    ``process_phase`` is the per-phase work (run tasks + resolve). The loop parks
    at a checkpoint **before** each phase, so a pause never interrupts mid-phase;
    a cancellation marks the whole tree ``Abandoned`` then re-raises.
    """
    try:
        for phase in plans_repo.list_phases(conn, plan_id):
            await control.checkpoint()  # park here while paused
            await process_phase(phase)
    except asyncio.CancelledError:
        abandon_tree(conn, plan_id)
        raise
