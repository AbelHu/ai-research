"""Plans / phases / tasks repository (design-spec §6B, §9; implementation-plan T6.1).

Typed rows + helpers for the complex-job hierarchy. ``create_plan_from_spec``
persists a validated `PlanSpec` (phases → tasks + deps) as a `New` plan tree;
status transitions are applied later via the lifecycle layer (T6.2). Task
``depends_on`` indices are validated against their phase and stored as the actual
``plan_tasks.id`` values so the runner can resolve them directly.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass

from app.advisor.schemas import PlanSpec


@dataclass(frozen=True)
class Plan:
    id: int
    job_id: int
    status: str
    approved_by: str | None
    resolved_by: str | None
    closed_by: str | None
    created_at: str

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> Plan:
        return cls(
            id=row["id"],
            job_id=row["job_id"],
            status=row["status"],
            approved_by=row["approved_by"],
            resolved_by=row["resolved_by"],
            closed_by=row["closed_by"],
            created_at=row["created_at"],
        )


@dataclass(frozen=True)
class Phase:
    id: int
    plan_id: int
    idx: int
    title: str | None
    status: str
    decline_count: int
    report_ref: str | None
    resolved_by: str | None
    signed_off_by: str | None
    created_at: str

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> Phase:
        return cls(
            id=row["id"],
            plan_id=row["plan_id"],
            idx=row["idx"],
            title=row["title"],
            status=row["status"],
            decline_count=row["decline_count"],
            report_ref=row["report_ref"],
            resolved_by=row["resolved_by"],
            signed_off_by=row["signed_off_by"],
            created_at=row["created_at"],
        )


@dataclass(frozen=True)
class PlanTask:
    id: int
    phase_id: int
    parent_task_id: int | None
    title: str | None
    status: str
    run_mode: str
    depends_on: list[int]
    owner_role: str | None
    created_at: str

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> PlanTask:
        return cls(
            id=row["id"],
            phase_id=row["phase_id"],
            parent_task_id=row["parent_task_id"],
            title=row["title"],
            status=row["status"],
            run_mode=row["run_mode"],
            depends_on=json.loads(row["depends_on_json"]) if row["depends_on_json"] else [],
            owner_role=row["owner_role"],
            created_at=row["created_at"],
        )


def create_plan(conn: sqlite3.Connection, *, job_id: int) -> Plan:
    """Insert an empty ``New`` plan for a job."""
    with conn:
        cur = conn.execute("INSERT INTO plans (job_id) VALUES (?)", (job_id,))
    plan = get_plan(conn, int(cur.lastrowid))
    assert plan is not None
    return plan


def create_plan_from_spec(conn: sqlite3.Connection, *, job_id: int, spec: PlanSpec) -> Plan:
    """Persist a validated `PlanSpec` as a ``New`` plan → phases → tasks tree.

    Each task's ``depends_on`` indices (earlier siblings in the same phase) are
    validated and rewritten to the concrete ``plan_tasks.id`` of those siblings,
    so the runner resolves dependencies without re-deriving indices.
    """
    with conn:
        cur = conn.execute("INSERT INTO plans (job_id) VALUES (?)", (job_id,))
        plan_id = int(cur.lastrowid)
        for p_idx, phase in enumerate(spec.phases):
            pcur = conn.execute(
                "INSERT INTO phases (plan_id, idx, title) VALUES (?, ?, ?)",
                (plan_id, p_idx, phase.title),
            )
            phase_id = int(pcur.lastrowid)
            task_ids: list[int] = []
            for t_idx, task in enumerate(phase.tasks):
                for dep in task.depends_on:
                    if dep < 0 or dep >= t_idx:
                        raise ValueError(
                            f"task {t_idx} in phase {p_idx} depends on invalid index {dep} "
                            "(must reference an earlier task in the same phase)"
                        )
                dep_ids = [task_ids[d] for d in task.depends_on]
                tcur = conn.execute(
                    "INSERT INTO plan_tasks "
                    "(phase_id, title, run_mode, depends_on_json, owner_role) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (phase_id, task.title, task.run_mode, json.dumps(dep_ids), "senior_worker"),
                )
                task_ids.append(int(tcur.lastrowid))
    plan = get_plan(conn, plan_id)
    assert plan is not None
    return plan


def get_plan(conn: sqlite3.Connection, plan_id: int) -> Plan | None:
    row = conn.execute("SELECT * FROM plans WHERE id = ?", (plan_id,)).fetchone()
    return Plan.from_row(row) if row else None


def get_plan_for_job(conn: sqlite3.Connection, job_id: int) -> Plan | None:
    row = conn.execute(
        "SELECT * FROM plans WHERE job_id = ? ORDER BY id LIMIT 1", (job_id,)
    ).fetchone()
    return Plan.from_row(row) if row else None


def list_phases(conn: sqlite3.Connection, plan_id: int) -> list[Phase]:
    rows = conn.execute(
        "SELECT * FROM phases WHERE plan_id = ? ORDER BY idx, id", (plan_id,)
    ).fetchall()
    return [Phase.from_row(r) for r in rows]


def list_tasks(conn: sqlite3.Connection, phase_id: int) -> list[PlanTask]:
    rows = conn.execute(
        "SELECT * FROM plan_tasks WHERE phase_id = ? ORDER BY id", (phase_id,)
    ).fetchall()
    return [PlanTask.from_row(r) for r in rows]


def get_phase(conn: sqlite3.Connection, phase_id: int) -> Phase | None:
    row = conn.execute("SELECT * FROM phases WHERE id = ?", (phase_id,)).fetchone()
    return Phase.from_row(row) if row else None


def get_task(conn: sqlite3.Connection, task_id: int) -> PlanTask | None:
    row = conn.execute("SELECT * FROM plan_tasks WHERE id = ?", (task_id,)).fetchone()
    return PlanTask.from_row(row) if row else None


# --- lifecycle-validated status setters (design-spec §6B; plan T6.2/T6.3) ---
# Each validates the (from -> to, actor) edge via `app.roles.lifecycle` before
# writing, so an illegal status change or wrong actor is rejected at the repo
# boundary (no silent corruption of the plan tree).


def set_plan_status(conn: sqlite3.Connection, plan_id: int, to: str, *, actor) -> Plan:
    from app.roles.lifecycle import Entity, Status, validate_transition

    plan = get_plan(conn, plan_id)
    if plan is None:
        raise ValueError(f"unknown plan: {plan_id}")
    validate_transition(Entity.plan, Status(plan.status), Status(to), actor)
    column = _signoff_column(actor, to)
    with conn:
        if column:
            conn.execute(
                f"UPDATE plans SET status = ?, {column} = ? WHERE id = ?",  # noqa: S608
                (to, actor.value, plan_id),
            )
        else:
            conn.execute("UPDATE plans SET status = ? WHERE id = ?", (to, plan_id))
    updated = get_plan(conn, plan_id)
    assert updated is not None
    return updated


def set_phase_status(
    conn: sqlite3.Connection,
    phase_id: int,
    to: str,
    *,
    actor,
    bump_decline: bool = False,
) -> Phase:
    from app.roles.lifecycle import Entity, Status, validate_transition

    phase = get_phase(conn, phase_id)
    if phase is None:
        raise ValueError(f"unknown phase: {phase_id}")
    validate_transition(Entity.phase, Status(phase.status), Status(to), actor)
    with conn:
        if bump_decline:
            conn.execute(
                "UPDATE phases SET status = ?, decline_count = decline_count + 1 WHERE id = ?",
                (to, phase_id),
            )
        else:
            conn.execute("UPDATE phases SET status = ? WHERE id = ?", (to, phase_id))
    updated = get_phase(conn, phase_id)
    assert updated is not None
    return updated


def set_task_status(conn: sqlite3.Connection, task_id: int, to: str, *, actor) -> PlanTask:
    from app.roles.lifecycle import Entity, Status, validate_transition

    task = get_task(conn, task_id)
    if task is None:
        raise ValueError(f"unknown task: {task_id}")
    validate_transition(Entity.task, Status(task.status), Status(to), actor)
    with conn:
        conn.execute("UPDATE plan_tasks SET status = ? WHERE id = ?", (to, task_id))
    updated = get_task(conn, task_id)
    assert updated is not None
    return updated


def set_phase_report_ref(conn: sqlite3.Connection, phase_id: int, report_ref: str) -> None:
    """Attach a phase's report reference (the Plan Expert's phase report, §6B)."""
    with conn:
        conn.execute("UPDATE phases SET report_ref = ? WHERE id = ?", (report_ref, phase_id))


def _signoff_column(actor, to: str) -> str | None:
    """The plans signoff column to stamp for a given transition (or None)."""
    if to == "Approved":
        return "approved_by"
    if to == "Resolved":
        return "resolved_by"
    if to == "Closed":
        return "closed_by"
    return None


def approve_plan(conn: sqlite3.Connection, plan_id: int, *, actor) -> Plan:
    """Approve a plan and cascade ``New -> Approved`` to its phases + tasks (§6B).

    Mirrors "plan approved" in the lifecycle: every phase and task drafted with
    the plan moves to ``Approved`` in the same transaction.
    """
    plan = set_plan_status(conn, plan_id, "Approved", actor=actor)
    for phase in list_phases(conn, plan_id):
        if phase.status == "New":
            set_phase_status(conn, phase.id, "Approved", actor=actor)
        for task in list_tasks(conn, phase.id):
            if task.status == "New":
                set_task_status(conn, task.id, "Approved", actor=actor)
    return plan
