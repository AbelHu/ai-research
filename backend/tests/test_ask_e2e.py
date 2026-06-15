"""End-to-end ask through the control loop with a fake provider (T4.6)."""

from __future__ import annotations

import json

import pytest

from app.advisor.wrapper import Advisor
from app.roles.control import ensure_owner, run_ask
from app.storage.db import connect
from app.storage.migrations import migrate
from app.storage.repos import ai_calls as ai_calls_repo
from app.storage.repos import memories as memories_repo
from app.storage.repos import requests as requests_repo
from app.storage.repos import role_messages as role_messages_repo
from app.storage.repos import steps as steps_repo
from tests.fakes import FakeProvider

ANALYSIS_ASK = json.dumps(
    {
        "belongs": True,
        "kind": "ask",
        "clarity": "clear",
        "complexity": "simple",
        "confidence": 0.95,
        "rationale": "a direct factual question",
    }
)
ANALYSIS_TASK = json.dumps(
    {
        "belongs": True,
        "kind": "task",
        "clarity": "clear",
        "complexity": "complex",
        "confidence": 0.9,
        "rationale": "multi-step work",
        "plan": {"phases": ["research", "compare"]},
    }
)
ANALYSIS_UNCLEAR = json.dumps(
    {
        "belongs": True,
        "kind": "ask",
        "clarity": "unclear",
        "complexity": "simple",
        "confidence": 0.4,
        "rationale": "ambiguous",
        "clarify": ["which thing do you mean?"],
    }
)
ANSWER = json.dumps(
    {
        "answer": "Paris is the capital of France.",
        "citations": [{"ref": "memory:1", "snippet": "capital of France is Paris"}],
        "confidence": 0.95,
    }
)


def _advisor(conn, *, planner: str, drafter: str = ANSWER) -> Advisor:
    providers = {
        "planner": FakeProvider(planner),
        "drafter": FakeProvider(drafter),
    }
    return Advisor(resolve_provider=lambda role: providers[role], conn=conn)


@pytest.fixture
def conn():
    c = connect()
    migrate(c)
    try:
        yield c
    finally:
        c.close()


def test_simple_ask_answered_end_to_end(conn) -> None:
    memories_repo.create_memory(conn, content="the capital of France is Paris")
    user_id = ensure_owner(conn)

    outcome = run_ask(
        conn,
        _advisor(conn, planner=ANALYSIS_ASK),
        "what is the capital of France?",
        user_id=user_id,
    )

    assert outcome.status == "answered"
    assert outcome.answer.answer.startswith("Paris")
    assert outcome.answer.citations[0].ref == "memory:1"
    assert f"/req {outcome.request.code}" in outcome.delivery
    # The source is surfaced to the user in the delivery (provenance, §7.1).
    assert "Sources:" in outcome.delivery
    assert "memory:1" in outcome.delivery


def test_full_trace_is_persisted(conn) -> None:
    user_id = ensure_owner(conn)
    outcome = run_ask(
        conn, _advisor(conn, planner=ANALYSIS_ASK), "capital of France?", user_id=user_id
    )
    request_id = outcome.request.id

    # role_messages: the full envelope chain in order.
    msgs = role_messages_repo.list_role_messages(conn, request_id)
    assert [m["action"] for m in msgs] == [
        "route_request",
        "analyze",
        "analysis_done",
        "answer_ask",
        "ask_done",
        "deliver",
    ]
    # Each non-first envelope is causally linked to the previous one.
    ids = [m["id"] for m in msgs]
    causes = [m["causation_id"] for m in msgs]
    assert causes[0] is None
    assert causes[1:] == ids[:-1]

    # jobs / steps / ai_calls all written.
    job = requests_repo.get_job_for_request(conn, request_id)
    assert job is not None and job.kind == "ask"
    steps = steps_repo.list_steps(conn, job.id)
    assert [s["skill_name"] for s in steps] == ["memory.search"]
    ai_calls = ai_calls_repo.list_ai_calls(conn, request_id)
    assert {c["role"] for c in ai_calls} == {"planner", "drafter"}


def test_unclear_ask_requests_clarification(conn) -> None:
    user_id = ensure_owner(conn)
    outcome = run_ask(
        conn, _advisor(conn, planner=ANALYSIS_UNCLEAR), "do the thing", user_id=user_id
    )

    assert outcome.status == "needs_clarification"
    assert outcome.clarify == ["which thing do you mean?"]
    # No answer drafted; trace stops at the clarify hand-off to the PM.
    actions = [m["action"] for m in role_messages_repo.list_role_messages(conn, outcome.request.id)]
    assert actions == ["route_request", "analyze", "analysis_done", "clarify"]


def test_complex_request_is_classified_but_not_executed(conn) -> None:
    user_id = ensure_owner(conn)
    outcome = run_ask(
        conn, _advisor(conn, planner=ANALYSIS_TASK), "compare three vendors", user_id=user_id
    )

    assert outcome.status == "planned"
    assert outcome.job_id is not None
    actions = [m["action"] for m in role_messages_repo.list_role_messages(conn, outcome.request.id)]
    assert actions == ["route_request", "analyze", "analysis_done", "review_plan"]


def test_owner_is_created_once(conn) -> None:
    first = ensure_owner(conn)
    second = ensure_owner(conn)
    assert first == second
    count = conn.execute("SELECT COUNT(*) FROM users WHERE is_owner = 1").fetchone()[0]
    assert count == 1
