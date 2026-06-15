"""Advisor wrapper tests — validate/repair/fallback/audit (T3.3-T3.8).

All offline: the advisor talks to a `FakeProvider`, never the network.
"""

from __future__ import annotations

import json

import pytest

from app.advisor.schemas import Analysis, AnswerDraft, Triage
from app.advisor.wrapper import Advisor, AdvisorValidationError
from app.security import REDACTED
from app.storage.db import connect
from app.storage.migrations import migrate
from app.storage.repos import ai_calls as ai_calls_repo
from app.storage.repos import requests as requests_repo
from tests.fakes import FakeProvider

# --- canned model outputs ---------------------------------------------------

VALID_TRIAGE = json.dumps(
    {
        "kind": "ask",
        "clarity": "clear",
        "complexity": "simple",
        "confidence": 0.9,
        "rationale": "a direct factual question",
    }
)
VALID_ANALYSIS = json.dumps(
    {
        "belongs": True,
        "kind": "task",
        "clarity": "clear",
        "complexity": "complex",
        "confidence": 0.8,
        "rationale": "multi-step comparison",
        "plan": {"phases": ["research", "compare", "recommend"]},
    }
)
VALID_ANSWER = json.dumps(
    {
        "answer": "Paris is the capital of France.",
        "citations": [{"ref": "memory:12", "snippet": "capital of France is Paris"}],
        "confidence": 0.95,
    }
)
MALFORMED = "sorry, I can't produce JSON right now"
ZERO_CITATION_ANSWER = json.dumps({"answer": "Paris.", "citations": [], "confidence": 0.5})


@pytest.fixture
def db():
    conn = connect()
    migrate(conn)
    req = requests_repo.create_request(conn)
    try:
        yield conn, req.id
    finally:
        conn.close()


def _advisor(conn, provider: FakeProvider) -> Advisor:
    return Advisor(resolve_provider=lambda role: provider, conn=conn)


# --- T3.3 / T3.6 core + triage ---------------------------------------------


def test_triage_returns_validated_object_and_audits(db) -> None:
    conn, request_id = db
    provider = FakeProvider(VALID_TRIAGE)
    advisor = _advisor(conn, provider)

    result = advisor.triage("what is 2+2?", request_id=request_id)

    assert isinstance(result, Triage)
    assert (result.kind, result.clarity, result.complexity) == ("ask", "clear", "simple")

    rows = ai_calls_repo.list_ai_calls(conn, request_id)
    assert len(rows) == 1
    row = rows[0]
    assert row["validation_status"] == "valid"
    assert row["template"] == "triage.classify@v1"
    assert row["model_id"] == "fake-model"
    assert row["role"] == "triage"
    assert row["prompt_ref"].startswith("sha256:")
    assert row["response_ref"].startswith("sha256:")
    assert row["latency_ms"] is not None
    assert len(provider.calls) == 1  # no repair needed


# --- T3.4 repair + fallback -------------------------------------------------


def test_malformed_then_valid_is_repaired(db) -> None:
    conn, request_id = db
    provider = FakeProvider([MALFORMED, VALID_TRIAGE])
    advisor = _advisor(conn, provider)

    result = advisor.triage("hello", request_id=request_id)

    assert result.kind == "ask"
    assert len(provider.calls) == 2  # initial + one repair
    row = ai_calls_repo.list_ai_calls(conn, request_id)[0]
    assert row["validation_status"] == "repaired"


def test_always_malformed_triage_uses_fallback(db) -> None:
    conn, request_id = db
    provider = FakeProvider(MALFORMED)  # repeats -> repair also fails
    advisor = _advisor(conn, provider)

    result = advisor.triage("hello", request_id=request_id)

    # Deterministic safe default: unclear + complex -> never auto-answers.
    assert (result.kind, result.clarity, result.complexity) == ("ask", "unclear", "complex")
    assert result.confidence == 0.0
    row = ai_calls_repo.list_ai_calls(conn, request_id)[0]
    assert row["validation_status"] == "fallback"


# --- T3.5 redaction on the wrapper path ------------------------------------


def test_outbound_prompt_is_redacted(db) -> None:
    conn, request_id = db
    provider = FakeProvider(VALID_TRIAGE)
    advisor = _advisor(conn, provider)

    secret = "ghp_0123456789abcdefghijklmnopqrstuvwxyzABCDEF"
    advisor.triage(f"my token is {secret}", request_id=request_id)

    sent = provider.calls[0].messages[0]["content"]
    assert secret not in sent
    assert REDACTED in sent


# --- T3.7 analyze -----------------------------------------------------------


def test_analyze_returns_validated_analysis(db) -> None:
    conn, request_id = db
    provider = FakeProvider(VALID_ANALYSIS)
    advisor = _advisor(conn, provider)

    result = advisor.analyze(text="compare vendors A/B/C", request_id=request_id)

    assert isinstance(result, Analysis)
    assert result.belongs is True
    assert result.kind == "task"
    assert result.plan is not None
    assert result.plan.phases == ["research", "compare", "recommend"]


def test_analyze_repairs_then_falls_back(db) -> None:
    conn, request_id = db
    # malformed forever -> repair fails -> deterministic clarify fallback.
    provider = FakeProvider(MALFORMED)
    advisor = _advisor(conn, provider)

    result = advisor.analyze(text="??", request_id=request_id)

    assert result.clarity == "unclear"
    assert result.clarify  # non-empty -> routes to ask_clarify
    row = ai_calls_repo.list_ai_calls(conn, request_id)[0]
    assert row["validation_status"] == "fallback"


# --- T3.8 answer ------------------------------------------------------------


def test_answer_returns_validated_answer_with_citation(db) -> None:
    conn, request_id = db
    provider = FakeProvider(VALID_ANSWER)
    advisor = _advisor(conn, provider)

    result = advisor.answer(
        text="what is the capital of France?",
        hits=[{"ref": "memory:12", "snippet": "capital of France is Paris"}],
        request_id=request_id,
    )

    assert isinstance(result, AnswerDraft)
    assert len(result.citations) >= 1
    assert result.citations[0].ref == "memory:12"


def test_zero_citation_answer_is_rejected_and_escalates(db) -> None:
    conn, request_id = db
    provider = FakeProvider(ZERO_CITATION_ANSWER)  # repeats -> repair also invalid
    advisor = _advisor(conn, provider)

    with pytest.raises(AdvisorValidationError):
        advisor.answer(text="what is the capital of France?", request_id=request_id)

    # The failed call is still audited.
    row = ai_calls_repo.list_ai_calls(conn, request_id)[0]
    assert row["validation_status"] == "failed"
    assert len(provider.calls) == 2  # initial + one repair, both rejected
