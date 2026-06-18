"""Tests for the coder-queue repo + the coder worker (dedicated codegen lane, P4).

Offline: the worker drains a real in-memory ``coder_queue``; the agentic Coder
loop is stubbed (``run_coder`` monkeypatched) so we test the worker's queue
bookkeeping + delivery without spawning the sandbox (that is covered by the
``test_coder_agent`` end-to-end test).
"""

from __future__ import annotations

import pytest

import app.cli.coderworker as coderworker
from app.channels.adapter import OutboundMessage
from app.coder.agent import CoderOutcome
from app.storage.db import connect
from app.storage.migrations import migrate
from app.storage.repos import coder_queue as cq
from app.storage.repos import requests as requests_repo


@pytest.fixture
def conn():
    c = connect()
    migrate(c)
    try:
        yield c
    finally:
        c.close()


def _feature_job(conn):
    req = requests_repo.create_request(conn, title="feature: add a tool")
    job = requests_repo.create_job(conn, request_id=req.id, kind="feature", complexity="complex")
    return req, job


class _Sink:
    def __init__(self) -> None:
        self.sent: list[OutboundMessage] = []

    def __call__(self, msg: OutboundMessage) -> None:
        self.sent.append(msg)


# --- repo -------------------------------------------------------------------


def test_enqueue_is_idempotent_and_claim(conn) -> None:
    req, job = _feature_job(conn)
    first = cq.enqueue(
        conn, job_id=job.id, request_id=req.id, job_code=req.code, goal="g", chat_id="1"
    )
    assert first.status == cq.PENDING and first.chat_id == "1"
    # Re-enqueue keeps the original row (idempotent per job_id).
    cq.enqueue(conn, job_id=job.id, request_id=req.id, job_code=req.code, goal="g2", chat_id="9")
    assert cq.get(conn, job.id).goal == "g"

    claimed = cq.claim_next(conn)
    assert claimed is not None and claimed.job_id == job.id
    assert claimed.status == cq.RUNNING and claimed.attempts == 1
    assert cq.claim_next(conn) is None  # nothing left pending
    assert cq.count_pending(conn) == 0


def test_mark_done_and_failed(conn) -> None:
    req, job = _feature_job(conn)
    cq.enqueue(conn, job_id=job.id, request_id=req.id, job_code=req.code, goal="g")
    cq.claim_next(conn)
    cq.mark_done(conn, job.id, skill_modules=["a.py"], validation={"summary": "import=ok"})
    done = cq.get(conn, job.id)
    assert done.status == cq.DONE
    assert done.skill_modules == ["a.py"]
    assert done.validation == {"summary": "import=ok"}

    req2, job2 = _feature_job(conn)
    cq.enqueue(conn, job_id=job2.id, request_id=req2.id, job_code=req2.code, goal="g")
    cq.claim_next(conn)
    cq.mark_failed(conn, job2.id, "nope", validation={"summary": "import=FAIL"})
    failed = cq.get(conn, job2.id)
    assert failed.status == cq.FAILED and failed.error == "nope"


# --- worker -----------------------------------------------------------------


def test_coder_worker_success_records_and_delivers(conn, monkeypatch) -> None:
    req, job = _feature_job(conn)
    cq.enqueue(
        conn,
        job_id=job.id,
        request_id=req.id,
        job_code=req.code,
        goal="build a doubler",
        channel="telegram",
        chat_id="42",
        reply_to_message_id="5",
    )
    monkeypatch.setattr(
        coderworker,
        "run_coder",
        lambda *a, **k: CoderOutcome(
            ok=True, job_code=req.code, iterations=1, skill_modules=["dbl.py"]
        ),
    )
    sink = _Sink()

    rc = coderworker.serve_coder_jobs(conn, advisor=None, send=sink, once=True)

    assert rc == 0
    row = cq.get(conn, job.id)
    assert row.status == cq.DONE and row.skill_modules == ["dbl.py"]
    assert len(sink.sent) == 1
    out = sink.sent[0]
    assert out.chat_id == "42" and out.reply_to_message_id == "5"
    assert "confirm" in out.text.lower() and req.code in out.text


def test_coder_worker_failure_records_and_notes(conn, monkeypatch) -> None:
    req, job = _feature_job(conn)
    cq.enqueue(
        conn, job_id=job.id, request_id=req.id, job_code=req.code, goal="g", chat_id="9"
    )
    monkeypatch.setattr(
        coderworker,
        "run_coder",
        lambda *a, **k: CoderOutcome(
            ok=False, job_code=req.code, iterations=2, error="validation failed after retries"
        ),
    )
    sink = _Sink()

    coderworker.serve_coder_jobs(conn, advisor=None, send=sink, once=True)

    row = cq.get(conn, job.id)
    assert row.status == cq.FAILED and "validation failed" in (row.error or "")
    assert len(sink.sent) == 1
    assert "couldn't" in sink.sent[0].text.lower() or "leaving it out" in sink.sent[0].text.lower()


def test_coder_worker_crash_marks_failed_and_keeps_going(conn, monkeypatch) -> None:
    req, job = _feature_job(conn)
    cq.enqueue(conn, job_id=job.id, request_id=req.id, job_code=req.code, goal="g")

    def _boom(*_a, **_k):
        raise RuntimeError("kaboom")

    monkeypatch.setattr(coderworker, "run_coder", _boom)

    rc = coderworker.serve_coder_jobs(conn, advisor=None, send=None, once=True)
    assert rc == 0  # the bad request didn't crash the worker
    assert cq.get(conn, job.id).status == cq.FAILED
