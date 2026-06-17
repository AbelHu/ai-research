"""Read-only web service layer (design-spec §11; implementation-plan P10).

Framework-agnostic data services behind the dashboard. Each function returns
**JSON-serializable** plain dicts so the thin HTTP layer (added once the web
framework is chosen) only has to serialize them:

* `request_overview` / `request_tree` — the Requests page: list + the full
  job → plan → phase → task tree with steps and `ai_calls` (§11, T10.2).
* `model_usage` — the System page: usage aggregated from `ai_calls` (T10.3).
* `system_metrics` — host CPU/mem/disk via the **stdlib** (no `psutil`); readers
  are injectable so it's deterministic + offline-testable (T10.3).
* `list_paired_accounts` / `revoke_account` — the Settings page over the P7
  allowlist (T10.5).

No web framework, no network — pure data assembly over the repos.
"""

from __future__ import annotations

import os
import shutil
import sqlite3
from collections.abc import Callable

from app.storage.repos import ai_calls as ai_calls_repo
from app.storage.repos import identities as identities_repo
from app.storage.repos import memories as memories_repo
from app.storage.repos import plans as plans_repo
from app.storage.repos import requests as requests_repo
from app.storage.repos import steps as steps_repo

# --- Requests page (T10.2) --------------------------------------------------


def request_overview(conn: sqlite3.Connection, *, limit: int = 100) -> list[dict]:
    """List requests newest-first for the index (id, code, title, status, state)."""
    return [
        {
            "id": r.id,
            "code": r.code,
            "title": r.title,
            "status": r.status,
            "state": r.state,
            "created_at": r.created_at,
        }
        for r in requests_repo.list_requests(conn, limit=limit)
    ]


def request_tree(conn: sqlite3.Connection, request_id: int) -> dict | None:
    """Assemble one request's job → plan → phase → task tree + steps + ai_calls.

    Returns ``None`` when the request id is unknown. The shape is intentionally
    flat + JSON-friendly so the dashboard can render the live process for a
    request (the §11 Requests page).
    """
    request = requests_repo.get_request(conn, request_id)
    if request is None:
        return None

    job = requests_repo.get_job_for_request(conn, request_id)
    plan = plans_repo.get_plan_for_job(conn, job.id) if job is not None else None

    phases: list[dict] = []
    if plan is not None:
        for phase in plans_repo.list_phases(conn, plan.id):
            tasks = [
                {
                    "id": t.id,
                    "title": t.title,
                    "status": t.status,
                    "run_mode": t.run_mode,
                    "depends_on": t.depends_on,
                    "owner_role": t.owner_role,
                    "parent_task_id": t.parent_task_id,
                }
                for t in plans_repo.list_tasks(conn, phase.id)
            ]
            phases.append(
                {
                    "id": phase.id,
                    "idx": phase.idx,
                    "title": phase.title,
                    "status": phase.status,
                    "decline_count": phase.decline_count,
                    "tasks": tasks,
                }
            )

    steps = []
    if job is not None:
        steps = [
            {
                "idx": row["idx"],
                "skill_name": row["skill_name"],
                "status": row["status"],
                "plan_task_id": row["plan_task_id"],
                "started_at": row["started_at"],
                "ended_at": row["ended_at"],
            }
            for row in steps_repo.list_steps(conn, job.id)
        ]

    ai_calls = [
        {
            "id": row["id"],
            "role": row["role"],
            "model_id": row["model_id"],
            "template": row["template"],
            "tokens": row["tokens"],
            "latency_ms": row["latency_ms"],
            "validation_status": row["validation_status"],
            "created_at": row["created_at"],
        }
        for row in ai_calls_repo.list_ai_calls(conn, request_id)
    ]

    return {
        "request": {
            "id": request.id,
            "code": request.code,
            "title": request.title,
            "status": request.status,
            "state": request.state,
            "created_at": request.created_at,
        },
        "job": (
            None
            if job is None
            else {
                "id": job.id,
                "kind": job.kind,
                "clarity": job.clarity,
                "complexity": job.complexity,
                "paused": job.paused,
            }
        ),
        "plan": (
            None
            if plan is None
            else {"id": plan.id, "status": plan.status, "created_at": plan.created_at}
        ),
        "phases": phases,
        "steps": steps,
        "ai_calls": ai_calls,
    }


# --- System page: model usage (T10.3) ---------------------------------------


def model_usage(conn: sqlite3.Connection) -> dict:
    """Aggregate `ai_calls` into per-model + per-status usage for the dashboard.

    All deterministic SQL aggregation over the audit table (§7): per model id a
    call count, summed tokens, and average latency; plus a validation-status
    breakdown and grand totals.
    """
    by_model = [
        {
            "model_id": row["model_id"],
            "calls": row["calls"],
            "tokens": row["tokens"],
            "avg_latency_ms": (round(row["avg_latency_ms"], 1) if row["avg_latency_ms"] else None),
        }
        for row in conn.execute(
            "SELECT model_id, COUNT(*) AS calls, "
            "       COALESCE(SUM(tokens), 0) AS tokens, AVG(latency_ms) AS avg_latency_ms "
            "FROM ai_calls GROUP BY model_id ORDER BY calls DESC, model_id"
        ).fetchall()
    ]

    by_status = {
        row["validation_status"]: row["n"]
        for row in conn.execute(
            "SELECT validation_status, COUNT(*) AS n FROM ai_calls GROUP BY validation_status"
        ).fetchall()
    }

    totals_row = conn.execute(
        "SELECT COUNT(*) AS calls, COALESCE(SUM(tokens), 0) AS tokens FROM ai_calls"
    ).fetchone()

    return {
        "total_calls": totals_row["calls"],
        "total_tokens": totals_row["tokens"],
        "by_model": by_model,
        "by_validation_status": by_status,
    }


# --- System page: host metrics (T10.3) --------------------------------------

MeminfoReader = Callable[[], "str | None"]
LoadAvg = Callable[[], "tuple[float, float, float] | None"]
DiskUsage = Callable[[str], object]


def _read_proc_meminfo() -> str | None:
    """Return the text of ``/proc/meminfo`` (Linux), or ``None`` if unavailable."""
    try:
        with open("/proc/meminfo", encoding="utf-8") as fh:
            return fh.read()
    except OSError:  # pragma: no cover - non-Linux / sandboxed
        return None


def _safe_getloadavg() -> tuple[float, float, float] | None:
    """1/5/15-minute load average, or ``None`` where the OS doesn't provide it."""
    try:
        return os.getloadavg()
    except (OSError, AttributeError):  # pragma: no cover - platform dependent
        return None


def _parse_meminfo(text: str) -> dict:
    """Parse the kB fields of ``/proc/meminfo`` into a small memory summary."""
    fields: dict[str, int] = {}
    for line in text.splitlines():
        key, _, rest = line.partition(":")
        parts = rest.split()
        if parts and parts[0].isdigit():
            fields[key.strip()] = int(parts[0]) * 1024  # kB → bytes
    total = fields.get("MemTotal")
    available = fields.get("MemAvailable")
    used = (total - available) if (total is not None and available is not None) else None
    percent = round(used / total * 100, 1) if (used is not None and total) else None
    return {"total": total, "available": available, "used": used, "percent": percent}


def system_metrics(
    *,
    path: str | None = None,
    read_meminfo: MeminfoReader = _read_proc_meminfo,
    loadavg: LoadAvg = _safe_getloadavg,
    disk_usage: DiskUsage = shutil.disk_usage,
) -> dict:
    """Host CPU-load / memory / disk via the stdlib (readers injectable for tests).

    Everything degrades gracefully: a field is ``None`` where the platform can't
    provide it (e.g. no ``/proc``), never an error. No ``psutil`` dependency.
    """
    target = path or os.getcwd()
    usage = disk_usage(target)
    disk = {
        "total": usage.total,
        "used": usage.used,
        "free": usage.free,
        "percent": round(usage.used / usage.total * 100, 1) if usage.total else None,
    }

    meminfo_text = read_meminfo()
    memory = (
        _parse_meminfo(meminfo_text)
        if meminfo_text
        else {
            "total": None,
            "available": None,
            "used": None,
            "percent": None,
        }
    )

    load = loadavg()
    cpu = {
        "load_1m": load[0] if load else None,
        "load_5m": load[1] if load else None,
        "load_15m": load[2] if load else None,
        "cpu_count": os.cpu_count(),
    }

    return {"cpu": cpu, "memory": memory, "disk": disk}


# --- Memory page: stored memories (§9, §9.1) --------------------------------

_MEMORY_PREVIEW_LEN = 280


def _preview(content: str | None) -> str | None:
    """A short, single-purpose preview of a memory's content for the list view."""
    if not content:
        return None
    text = content.strip()
    if len(text) <= _MEMORY_PREVIEW_LEN:
        return text
    return text[:_MEMORY_PREVIEW_LEN].rstrip() + "…"


def memories_overview(conn: sqlite3.Connection, *, limit: int = 200) -> list[dict]:
    """List **active** memories newest-first for the dashboard Memory page (§9.1).

    Returns JSON-friendly rows: the summary (or a content preview) plus the
    recall signals the weighting uses (importance, confidence, use_count,
    retention_class). Archived/dropped (cold) memories stay out of the live
    view, mirroring recall.
    """
    memories = sorted(memories_repo.list_active(conn), key=lambda m: m.id, reverse=True)[:limit]
    return [
        {
            "id": m.id,
            "kind": m.kind,
            "entity_key": m.entity_key,
            "summary": m.summary,
            "preview": _preview(m.content),
            "importance": m.importance,
            "confidence": m.confidence,
            "use_count": m.use_count,
            "retention_class": m.retention_class,
            "last_used_at": m.last_used_at,
            "created_at": m.created_at,
        }
        for m in memories
    ]


# --- Settings page: paired accounts (T10.5) ---------------------------------


def list_paired_accounts(conn: sqlite3.Connection) -> list[dict]:
    """List the channel accounts on the allowlist (paired + revoked), for Settings."""
    accounts: list[dict] = []
    for state in ("paired", "revoked"):
        for ident in identities_repo.list_identities(conn, state=state):
            accounts.append(
                {
                    "channel": ident.channel,
                    "channel_user_id": ident.channel_user_id,
                    "state": ident.state,
                    "paired_via": ident.paired_via,
                    "paired_at": ident.paired_at,
                }
            )
    return accounts


def revoke_account(conn: sqlite3.Connection, channel: str, channel_user_id: str) -> bool:
    """Revoke a paired account from the web Settings page (calls the P7 repo)."""
    return identities_repo.revoke_identity(conn, channel, channel_user_id)
