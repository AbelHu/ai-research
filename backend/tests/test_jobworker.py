"""Tests for the job-queue repo + background worker (service slice B2).

Offline: the worker drains a real in-memory queue and runs planned jobs through
a per-role `FakeProvider`; deliveries are captured by a fake sink (no network).
"""

from __future__ import annotations

import json

import pytest

import app.cli.jobworker as jobworker
from app.advisor.wrapper import Advisor
from app.channels.adapter import OutboundMessage
from app.cli.jobworker import serve_jobs
from app.roles.control import ensure_owner
from app.storage.db import connect
from app.storage.migrations import migrate
from app.storage.repos import job_queue as jq
from app.storage.repos import plans as plans_repo
from app.storage.repos import requests as requests_repo
from tests.fakes import FakeProvider


def _plan_json(*titles: str) -> str:
    return json.dumps(
        {
            "phases": [
                {
                    "title": t,
                    "tasks": [{"title": f"do {t}", "depends_on": [], "run_mode": "serial"}],
                }
                for t in titles
            ]
        }
    )


APPROVE = json.dumps({"decision": "approve", "comments": []})
SEARCH = json.dumps({"skill": "memory.search", "params": {"query": "x"}, "rationale": "r"})
# Stops the bounded task loop after one executed action ([SEARCH, DONE] per task).
DONE = json.dumps({"skill": "memory.search", "params": {}, "rationale": "done", "done": True})


@pytest.fixture
def conn():
    c = connect()
    migrate(c)
    try:
        yield c
    finally:
        c.close()


def _planned_job(conn, *, kind="task"):
    req = requests_repo.create_request(conn, title="compare vendors")
    job = requests_repo.create_job(conn, request_id=req.id, kind=kind, complexity="complex")
    return req, job


# --- repo -------------------------------------------------------------------


def test_enqueue_is_idempotent(conn) -> None:
    _req, job = _planned_job(conn)
    first = jq.enqueue(conn, job.id, channel="telegram", chat_id="42", reply_to_message_id="7")
    assert first.status == jq.PENDING
    assert first.chat_id == "42"
    # Re-enqueue doesn't duplicate or reset the row.
    jq.enqueue(conn, job.id, channel="telegram", chat_id="999")
    assert len(jq.list_by_status(conn, jq.PENDING)) == 1
    assert jq.get(conn, job.id).chat_id == "42"  # original coords kept


def test_claim_next_moves_pending_to_running(conn) -> None:
    _req, job = _planned_job(conn)
    jq.enqueue(conn, job.id)
    claimed = jq.claim_next(conn)
    assert claimed is not None and claimed.job_id == job.id
    assert claimed.status == jq.RUNNING
    assert claimed.attempts == 1
    # Nothing else pending now.
    assert jq.claim_next(conn) is None
    assert jq.count_pending(conn) == 0


def test_claim_next_is_fifo(conn) -> None:
    _r1, j1 = _planned_job(conn)
    _r2, j2 = _planned_job(conn)
    jq.enqueue(conn, j1.id)
    jq.enqueue(conn, j2.id)
    assert jq.claim_next(conn).job_id == j1.id  # oldest first
    assert jq.claim_next(conn).job_id == j2.id


def test_mark_done_and_failed(conn) -> None:
    _req, job = _planned_job(conn)
    jq.enqueue(conn, job.id)
    jq.claim_next(conn)
    jq.mark_done(conn, job.id, "delivered text")
    row = jq.get(conn, job.id)
    assert row.status == jq.DONE and row.result == "delivered text"

    _r2, job2 = _planned_job(conn)
    jq.enqueue(conn, job2.id)
    jq.claim_next(conn)
    jq.mark_failed(conn, job2.id, "boom")
    assert jq.get(conn, job2.id).status == jq.FAILED
    assert jq.get(conn, job2.id).error == "boom"


# --- worker -----------------------------------------------------------------


class _Sink:
    def __init__(self) -> None:
        self.sent: list[OutboundMessage] = []

    def __call__(self, msg: OutboundMessage) -> None:
        self.sent.append(msg)


def _advisor(conn, responses: list[str]) -> Advisor:
    provider = FakeProvider(responses)
    return Advisor(resolve_provider=lambda _role: provider, conn=conn)


def test_worker_runs_job_and_delivers_to_chat(conn) -> None:
    ensure_owner(conn)
    _req, job = _planned_job(conn)
    jq.enqueue(conn, job.id, channel="telegram", chat_id="4242", reply_to_message_id="7")
    advisor = _advisor(conn, [_plan_json("Research"), APPROVE, SEARCH, DONE, APPROVE])
    sink = _Sink()

    rc = serve_jobs(conn, advisor, sink, once=True)

    assert rc == 0
    # The job completed and was delivered to the originating chat, quoting it.
    assert jq.get(conn, job.id).status == jq.DONE
    plan = plans_repo.get_plan_for_job(conn, job.id)
    assert plan.status == "Resolved"
    assert len(sink.sent) == 1
    out = sink.sent[0]
    assert out.channel == "telegram" and out.chat_id == "4242"
    assert out.reply_to_message_id == "7"  # quotes the user's original message
    assert "Completed phases" in out.text


def test_worker_failure_marks_failed_and_keeps_going(conn) -> None:
    ensure_owner(conn)
    _r1, bad = _planned_job(conn)
    _r2, good = _planned_job(conn)
    jq.enqueue(conn, bad.id, channel="telegram", chat_id="1")
    jq.enqueue(conn, good.id, channel="telegram", chat_id="2")
    sink = _Sink()

    # First job's plan can't be drafted (unparseable) → fails; second succeeds.
    advisor = _advisor(
        conn, ["(not valid json)", "(still bad)", _plan_json("P"), APPROVE, SEARCH, DONE, APPROVE]
    )
    serve_jobs(conn, advisor, sink, once=True)

    assert jq.get(conn, bad.id).status == jq.FAILED  # recorded, not crashed
    assert jq.get(conn, good.id).status == jq.DONE  # worker kept going
    assert [m.chat_id for m in sink.sent] == ["2"]  # only the good job delivered


def test_worker_once_returns_when_drained(conn) -> None:
    # No jobs queued → once mode returns immediately without sleeping.
    advisor = _advisor(conn, ["{}"])
    slept: list[float] = []
    rc = serve_jobs(conn, advisor, None, once=True, on_idle_sleep=slept.append)
    assert rc == 0
    assert slept == []

def test_worker_retries_transient_failure_then_succeeds(conn, monkeypatch) -> None:
    ensure_owner(conn)
    _req, job = _planned_job(conn)
    jq.enqueue(conn, job.id, channel="telegram", chat_id="22")
    sink = _Sink()

    calls = {"n": 0}

    class _Outcome:
        status = "completed"
        delivery = "done"

    def _flaky_execute(_conn, _advisor, *, job_id, user_id=None, delivery_coords=None):
        calls["n"] += 1
        if calls["n"] == 1:
            raise TimeoutError("request timed out")
        return _Outcome()

    monkeypatch.setattr(jobworker, "execute_planned_job", _flaky_execute)
    # Allow one retry after first transient failure.
    monkeypatch.setattr(jobworker, "get_policies", lambda: type("P", (), {"max_job_retries": 1})())

    advisor = _advisor(conn, ["{}"])
    serve_jobs(conn, advisor, sink, once=True)

    row = jq.get(conn, job.id)
    assert row.status == jq.DONE
    assert row.attempts == 2  # first claim failed transiently, second succeeded
    assert len(sink.sent) == 1 and sink.sent[0].chat_id == "22"


def test_worker_marks_failed_after_retry_budget_exhausted(conn, monkeypatch) -> None:
    ensure_owner(conn)
    _req, job = _planned_job(conn)
    jq.enqueue(conn, job.id)

    def _always_timeout(_conn, _advisor, *, job_id, user_id=None, delivery_coords=None):
        raise TimeoutError("request timed out")

    monkeypatch.setattr(jobworker, "execute_planned_job", _always_timeout)
    # Zero retries: first transient failure becomes terminal.
    monkeypatch.setattr(jobworker, "get_policies", lambda: type("P", (), {"max_job_retries": 0})())

    advisor = _advisor(conn, ["{}"])
    serve_jobs(conn, advisor, None, once=True)

    row = jq.get(conn, job.id)
    assert row.status == jq.FAILED
    assert row.attempts == 1
