"""Tests for the Junior Worker ask path (implementation-plan T4.5)."""

from __future__ import annotations

import json

import pytest

from app.advisor.schemas import AnswerDraft
from app.advisor.wrapper import Advisor
from app.roles.envelope import Action, Role
from app.roles.junior import answer_ask
from app.storage.db import connect
from app.storage.migrations import migrate
from app.storage.repos import memories as memories_repo
from app.storage.repos import requests as requests_repo
from app.storage.repos import steps as steps_repo
from tests.fakes import FakeProvider

VALID_ANSWER = json.dumps(
    {
        "answer": "Paris is the capital of France.",
        "citations": [{"ref": "memory:1", "snippet": "capital of France is Paris"}],
        "confidence": 0.95,
    }
)


@pytest.fixture
def job_card():
    conn = connect()
    migrate(conn)
    req = requests_repo.create_request(conn, title="capital of France?")
    job = requests_repo.create_job(conn, request_id=req.id, kind="ask", complexity="simple")
    card = {
        "request_id": req.id,
        "request_code": req.code,
        "title": req.title,
        "text": "what is the capital of France?",
        "append": False,
    }
    try:
        yield conn, job.id, card
    finally:
        conn.close()


def test_answer_ask_returns_validated_answer(job_card) -> None:
    conn, job_id, card = job_card
    memories_repo.create_memory(conn, content="the capital of France is Paris")
    advisor = Advisor(resolve_provider=lambda role: FakeProvider(VALID_ANSWER), conn=conn)

    result = answer_ask(conn, advisor, card, user_id=1, job_id=job_id)

    assert isinstance(result.answer, AnswerDraft)
    assert result.answer.citations[0].ref == "memory:1"
    # Emits ask_done back to the Boss.
    assert result.envelope.action is Action.ask_done
    assert result.envelope.to_role is Role.boss
    assert result.envelope.job_id == job_id
    assert result.envelope.payload["answer"]["answer"].startswith("Paris")


def test_answer_ask_records_a_search_step(job_card) -> None:
    conn, job_id, card = job_card
    advisor = Advisor(resolve_provider=lambda role: FakeProvider(VALID_ANSWER), conn=conn)

    answer_ask(conn, advisor, card, user_id=1, job_id=job_id)

    # memory.search ran through the runtime → exactly one recorded step.
    steps = steps_repo.list_steps(conn, job_id)
    assert len(steps) == 1
    assert steps[0]["skill_name"] == "memory.search"
    assert steps[0]["status"] == "ok"
