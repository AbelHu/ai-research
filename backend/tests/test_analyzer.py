"""Tests for the Analyzer validation + classification (implementation-plan T4.4)."""

from __future__ import annotations

import json

import pytest

from app.advisor.wrapper import Advisor
from app.roles.analyzer import analyze
from app.roles.envelope import Action, Role
from app.storage.db import connect
from app.storage.migrations import migrate
from app.storage.repos import requests as requests_repo
from tests.fakes import FakeProvider


def _analysis(**over) -> str:
    base = {
        "belongs": True,
        "kind": "ask",
        "clarity": "clear",
        "complexity": "simple",
        "confidence": 0.9,
        "rationale": "a direct question",
        "plan": None,
        "clarify": None,
    }
    base.update(over)
    return json.dumps(base)


@pytest.fixture
def db():
    conn = connect()
    migrate(conn)
    try:
        yield conn
    finally:
        conn.close()


def _card(conn, *, text: str, append: bool = False) -> dict:
    req = requests_repo.create_request(conn, title=text[:40])
    return {
        "request_id": req.id,
        "request_code": req.code,
        "title": req.title,
        "text": text,
        "append": append,
    }


def _advisor(conn, canned: str) -> Advisor:
    return Advisor(resolve_provider=lambda role: FakeProvider(canned), conn=conn)


def test_clear_ask_routes_to_answer_ask(db) -> None:
    card = _card(db, text="what is 2+2?")
    result = analyze(db, _advisor(db, _analysis()), card)

    assert result.verdict == "answer_ask"
    assert result.envelope.action is Action.analysis_done
    assert result.envelope.to_role is Role.boss
    assert result.envelope.payload["verdict"] == "answer_ask"
    # A job carrying the kind is minted for the work path.
    assert result.job_id is not None
    job = requests_repo.get_job(db, result.job_id)
    assert job.kind == "ask"
    assert job.complexity == "simple"


def test_complex_task_routes_to_plan_ready(db) -> None:
    card = _card(db, text="compare three vendors and recommend one")
    canned = _analysis(kind="task", complexity="complex", plan={"phases": ["research", "compare"]})
    result = analyze(db, _advisor(db, canned), card)

    assert result.verdict == "plan_ready"
    assert requests_repo.get_job(db, result.job_id).kind == "task"


def test_unclear_request_routes_to_clarify(db) -> None:
    card = _card(db, text="do the thing")
    canned = _analysis(clarity="unclear", clarify=["which thing do you mean?"])
    result = analyze(db, _advisor(db, canned), card)

    assert result.verdict == "ask_clarify"
    assert result.job_id is None  # no job until it's clear
    assert result.envelope.payload["clarify"] == ["which thing do you mean?"]


def test_wrong_append_rejected_once_then_clarifies(db) -> None:
    card = _card(db, text="unrelated note", append=True)
    canned = _analysis(belongs=False)

    # First pass: retries remain → reject the append for re-association.
    first = analyze(db, _advisor(db, canned), card, reroute_count=0, max_append_reroutes=1)
    assert first.verdict == "append_rejected"
    assert first.job_id is None

    # After the bounded retry is exhausted → defer to the user (clarify).
    second = analyze(db, _advisor(db, canned), card, reroute_count=1, max_append_reroutes=1)
    assert second.verdict == "ask_clarify"


def test_append_that_belongs_proceeds(db) -> None:
    card = _card(db, text="also add pricing", append=True)
    result = analyze(db, _advisor(db, _analysis(belongs=True)), card)
    assert result.verdict == "answer_ask"


def test_domain_is_carried_onto_card_and_payload(db) -> None:
    # The Analyzer advises the work domain; deterministic code carries it to the
    # downstream worker (on the card) so the tool catalog can be gated (§8.6).
    card = _card(db, text="refactor this function")
    result = analyze(db, _advisor(db, _analysis(domain="coding")), card)

    assert result.analysis.domain == "coding"
    assert result.envelope.payload["domain"] == "coding"
    assert card["domain"] == "coding"


def test_domain_defaults_to_general_when_model_omits_it(db) -> None:
    # Older/omitted replies still validate; the safe default is "general".
    card = _card(db, text="what is 2+2?")
    result = analyze(db, _advisor(db, _analysis()), card)

    assert result.analysis.domain == "general"
    assert result.envelope.payload["domain"] == "general"

