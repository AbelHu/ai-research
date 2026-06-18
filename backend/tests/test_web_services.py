"""Tests for the read-only web service layer (implementation-plan P10).

Offline + deterministic: seed a request/job/plan/steps/ai_calls + identities,
then assert the dashboard services assemble + aggregate them correctly. System
metrics use injected readers so they never depend on the host.
"""

from __future__ import annotations

import pytest

from app.advisor.schemas import PhaseSpec, PlanSpec, TaskSpec
from app.storage.db import connect
from app.storage.migrations import migrate
from app.storage.repos import ai_calls as ai_calls_repo
from app.storage.repos import api_usage as api_usage_repo
from app.storage.repos import identities as identities_repo
from app.storage.repos import job_queue as job_queue_repo
from app.storage.repos import memories as memories_repo
from app.storage.repos import plans as plans_repo
from app.storage.repos import requests as requests_repo
from app.storage.repos import steps as steps_repo
from app.web import services


@pytest.fixture
def conn():
    c = connect()
    migrate(c)
    try:
        yield c
    finally:
        c.close()


def _seed_request_with_job(conn):
    req = requests_repo.create_request(conn, title="compare vendors")
    job = requests_repo.create_job(conn, request_id=req.id, kind="task", complexity="complex")
    spec = PlanSpec(
        phases=[
            PhaseSpec(title="Research", tasks=[TaskSpec(title="gather"), TaskSpec(title="read")]),
            PhaseSpec(title="Recommend", tasks=[TaskSpec(title="decide")]),
        ]
    )
    plans_repo.create_plan_from_spec(conn, job_id=job.id, spec=spec)
    steps_repo.record_step(conn, job_id=job.id, skill_name="memory.search", status="ok")
    ai_calls_repo.record_ai_call(
        conn,
        request_id=req.id,
        job_id=job.id,
        role="planner",
        model_id="gpt-4o",
        template="analyzer.analyze@v1",
        tokens=300,
        latency_ms=2000,
        validation_status="valid",
    )
    return req, job


# --- Requests page (T10.2) --------------------------------------------------


def test_request_overview_lists_newest_first(conn) -> None:
    requests_repo.create_request(conn, title="first")
    requests_repo.create_request(conn, title="second")
    overview = services.request_overview(conn)
    assert [r["title"] for r in overview] == ["second", "first"]
    assert all({"id", "code", "title", "status", "state"} <= r.keys() for r in overview)


def test_request_tree_assembles_job_plan_phases_tasks(conn) -> None:
    req, job = _seed_request_with_job(conn)
    tree = services.request_tree(conn, req.id)

    assert tree is not None
    assert tree["request"]["id"] == req.id
    assert tree["job"]["kind"] == "task"
    assert tree["plan"]["status"] == "New"
    # Phases in order, each with its tasks.
    assert [p["title"] for p in tree["phases"]] == ["Research", "Recommend"]
    assert [t["title"] for t in tree["phases"][0]["tasks"]] == ["gather", "read"]
    # Steps + ai_calls attached.
    assert [s["skill_name"] for s in tree["steps"]] == ["memory.search"]
    assert tree["ai_calls"][0]["model_id"] == "gpt-4o"


def test_request_tree_unknown_id_is_none(conn) -> None:
    assert services.request_tree(conn, 9999) is None


def test_request_tree_without_job_has_empty_branches(conn) -> None:
    req = requests_repo.create_request(conn, title="just an ask")
    tree = services.request_tree(conn, req.id)
    assert tree is not None
    assert tree["job"] is None
    assert tree["plan"] is None
    assert tree["phases"] == []
    assert tree["steps"] == []


def test_job_queue_overview_includes_attempts_and_errors(conn) -> None:
    req1, job1 = _seed_request_with_job(conn)
    req2, job2 = _seed_request_with_job(conn)
    req3, job3 = _seed_request_with_job(conn)

    job_queue_repo.enqueue(conn, job1.id)
    job_queue_repo.enqueue(conn, job2.id)
    job_queue_repo.enqueue(conn, job3.id)
    job_queue_repo.claim_next(conn)
    job_queue_repo.requeue_pending(conn, job1.id, "request timed out")
    job_queue_repo.claim_next(conn)
    job_queue_repo.mark_done(conn, job1.id, "ok")
    job_queue_repo.claim_next(conn)
    job_queue_repo.mark_failed(conn, job2.id, "bad request")

    overview = services.job_queue_overview(conn)
    assert overview["total_jobs"] == 3
    assert overview["by_status"] == {
        "pending": 1,
        "running": 0,
        "done": 1,
        "failed": 1,
    }

    jobs = {row["job_id"]: row for row in overview["jobs"]}
    assert jobs[job1.id]["attempts"] == 2
    assert jobs[job1.id]["status"] == "done"
    assert jobs[job2.id]["status"] == "failed"
    assert jobs[job2.id]["error"] == "bad request"
    assert jobs[job3.id]["request_title"] == req3.title


def test_coder_queue_overview_reports_status_and_skills(conn) -> None:
    from app.storage.repos import coder_queue as cq

    req = requests_repo.create_request(conn, title="feature: add a tool")
    job = requests_repo.create_job(conn, request_id=req.id, kind="feature", complexity="complex")
    cq.enqueue(conn, job_id=job.id, request_id=req.id, job_code=req.code, goal="build it")
    cq.claim_next(conn)
    cq.mark_done(
        conn,
        job.id,
        skill_modules=["dbl.py"],
        validation={"summary": "import=ok lint=ok tests=ok", "iterations": 1},
    )

    overview = services.coder_queue_overview(conn)
    assert overview["total"] == 1
    assert overview["by_status"]["done"] == 1
    row = overview["jobs"][0]
    assert row["skill_modules"] == ["dbl.py"]
    assert "import=ok" in row["validation_summary"]
    assert row["request_code"] == req.code


# --- System page: model usage (T10.3) ---------------------------------------


def test_model_usage_aggregates_ai_calls(conn) -> None:
    req = requests_repo.create_request(conn, title="x")
    for model, tokens, latency, status in [
        ("gpt-4o", 100, 1000, "valid"),
        ("gpt-4o", 200, 3000, "repaired"),
        ("gpt-4o-mini", 50, 500, "valid"),
    ]:
        ai_calls_repo.record_ai_call(
            conn,
            request_id=req.id,
            model_id=model,
            tokens=tokens,
            latency_ms=latency,
            validation_status=status,
        )

    usage = services.model_usage(conn)
    assert usage["total_calls"] == 3
    assert usage["total_tokens"] == 350
    by_model = {m["model_id"]: m for m in usage["by_model"]}
    assert by_model["gpt-4o"]["calls"] == 2
    assert by_model["gpt-4o"]["tokens"] == 300
    assert by_model["gpt-4o"]["avg_latency_ms"] == 2000.0
    assert usage["by_validation_status"] == {"valid": 2, "repaired": 1}
    assert usage["web_search_credits_used_today"] == 0
    assert usage["web_search_credits_total"] == 0


def test_model_usage_includes_tavily_credit_usage(conn) -> None:
    api_usage_repo.increment(conn, "tavily", amount=3)
    usage = services.model_usage(conn)
    assert usage["web_search_credits_used_today"] == 3
    assert usage["web_search_credits_total"] == 3


def test_model_usage_empty(conn) -> None:
    usage = services.model_usage(conn)
    assert usage["total_calls"] == 0
    assert usage["total_tokens"] == 0
    assert usage["by_model"] == []
    assert usage["web_search_credits_used_today"] == 0
    assert usage["web_search_credits_total"] == 0


def test_usage_aggregate_groups_by_day_and_custom_range(conn) -> None:
    req = requests_repo.create_request(conn, title="usage range")
    first = ai_calls_repo.record_ai_call(
        conn,
        request_id=req.id,
        model_id="gpt-4o",
        tokens=100,
    )
    second = ai_calls_repo.record_ai_call(
        conn,
        request_id=req.id,
        model_id="gpt-4o-mini",
        tokens=50,
    )
    with conn:
        conn.execute("UPDATE ai_calls SET created_at = ? WHERE id = ?", ("2026-06-01", first))
        conn.execute("UPDATE ai_calls SET created_at = ? WHERE id = ?", ("2026-06-02", second))
    api_usage_repo.increment(conn, "tavily", day="2026-06-01", amount=2)
    api_usage_repo.increment(conn, "tavily", day="2026-06-02", amount=1)

    usage = services.usage_aggregate(
        conn,
        bucket="day",
        start="2026-06-01",
        end="2026-06-02",
    )
    assert usage["bucket"] == "day"
    assert usage["range"] == {"start": "2026-06-01", "end": "2026-06-02"}
    assert usage["totals"]["tokens"] == 150
    assert usage["totals"]["tavily_credits"] == 3
    assert usage["totals"]["model_calls"] == 2
    assert len(usage["credits_by_bucket"]) == 2
    assert len(usage["tokens_by_bucket"]) == 2


def test_usage_aggregate_groups_by_month(conn) -> None:
    req = requests_repo.create_request(conn, title="usage month")
    first = ai_calls_repo.record_ai_call(
        conn,
        request_id=req.id,
        model_id="gpt-4o",
        tokens=70,
    )
    second = ai_calls_repo.record_ai_call(
        conn,
        request_id=req.id,
        model_id="gpt-4o",
        tokens=30,
    )
    with conn:
        conn.execute("UPDATE ai_calls SET created_at = ? WHERE id = ?", ("2026-05-15", first))
        conn.execute("UPDATE ai_calls SET created_at = ? WHERE id = ?", ("2026-06-15", second))
    api_usage_repo.increment(conn, "tavily", day="2026-05-15", amount=1)
    api_usage_repo.increment(conn, "tavily", day="2026-06-15", amount=2)

    usage = services.usage_aggregate(
        conn,
        bucket="month",
        start="2026-05-01",
        end="2026-06-30",
    )
    assert usage["bucket"] == "month"
    assert [r["bucket"] for r in usage["credits_by_bucket"]] == ["2026-05", "2026-06"]
    assert usage["totals"]["tokens"] == 100
    assert usage["totals"]["tavily_credits"] == 3


# --- System page: host metrics (T10.3) --------------------------------------


class _FakeUsage:
    def __init__(self, total, used, free):
        self.total = total
        self.used = used
        self.free = free


def test_system_metrics_with_injected_readers() -> None:
    meminfo = "MemTotal:       16000 kB\nMemAvailable:    4000 kB\nSwapTotal: 0 kB\n"
    metrics = services.system_metrics(
        read_meminfo=lambda: meminfo,
        loadavg=lambda: (0.5, 0.4, 0.3),
        disk_usage=lambda _p: _FakeUsage(total=1000, used=250, free=750),
    )
    assert metrics["disk"] == {"total": 1000, "used": 250, "free": 750, "percent": 25.0}
    # 16000 kB total, 4000 kB available → 12000 kB used = 75%.
    assert metrics["memory"]["total"] == 16000 * 1024
    assert metrics["memory"]["used"] == 12000 * 1024
    assert metrics["memory"]["percent"] == 75.0
    assert metrics["cpu"]["load_1m"] == 0.5
    assert metrics["cpu"]["cpu_count"] == __import__("os").cpu_count()


def test_system_metrics_degrades_without_proc() -> None:
    metrics = services.system_metrics(
        read_meminfo=lambda: None,
        loadavg=lambda: None,
        disk_usage=lambda _p: _FakeUsage(total=0, used=0, free=0),
    )
    assert metrics["memory"] == {"total": None, "available": None, "used": None, "percent": None}
    assert metrics["cpu"]["load_1m"] is None
    assert metrics["disk"]["percent"] is None  # total 0 → no percent (no ZeroDivision)


# --- Settings page: paired accounts (T10.5) ---------------------------------


def test_list_and_revoke_paired_accounts(conn) -> None:
    owner_id = identities_repo.ensure_owner(conn)
    identities_repo.bind_identity(
        conn, user_id=owner_id, channel="telegram", channel_user_id="42", paired_via="host_code"
    )

    accounts = services.list_paired_accounts(conn)
    assert len(accounts) == 1
    assert accounts[0]["channel"] == "telegram"
    assert accounts[0]["state"] == "paired"

    assert services.revoke_account(conn, "telegram", "42") is True
    after = {a["channel_user_id"]: a for a in services.list_paired_accounts(conn)}
    assert after["42"]["state"] == "revoked"
    # Revoking again is a no-op.
    assert services.revoke_account(conn, "telegram", "42") is False


# --- Memory page (§9.1) -----------------------------------------------------


def test_memories_overview_lists_active_only(conn) -> None:
    active = memories_repo.create_memory(
        conn, content="user prefers dark mode", summary="prefers dark mode", kind="preference"
    )
    archived = memories_repo.create_memory(conn, content="stale fact", kind="fact")
    memories_repo.archive_memory(conn, archived.id)

    rows = services.memories_overview(conn)
    by_id = {r["id"]: r for r in rows}
    assert archived.id not in by_id  # cold (archived) memories stay out of the live view
    assert by_id[active.id]["summary"] == "prefers dark mode"
    assert by_id[active.id]["kind"] == "preference"
    assert by_id[active.id]["preview"] == "user prefers dark mode"


def test_memories_overview_newest_first_and_preview_truncates(conn) -> None:
    memories_repo.create_memory(conn, content="first", kind="fact")
    long_text = "x" * 400
    newest = memories_repo.create_memory(conn, content=long_text, kind="fact")

    rows = services.memories_overview(conn)
    assert rows[0]["id"] == newest.id  # newest first
    assert rows[0]["preview"].endswith("…")
    assert len(rows[0]["preview"]) <= services._MEMORY_PREVIEW_LEN + 1  # +1 for the ellipsis
