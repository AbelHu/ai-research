"""Test the Junior's research loop: gather web/live context when memory is empty.

Offline: the advisor (per-role `FakeProvider`) proposes `data.weather`, whose
Open-Meteo seam is monkeypatched. Proves an unanswerable-from-memory ask (e.g.
weather) now gets answered + cited from a read tool instead of dead-ending.
"""

from __future__ import annotations

import json

import pytest

from app.advisor.wrapper import Advisor
from app.roles import junior
from app.skills import data
from app.storage.db import connect
from app.storage.migrations import migrate
from app.storage.repos import requests as requests_repo
from app.storage.repos import steps as steps_repo
from tests.fakes import FakeProvider

WEATHER_ACTION = json.dumps(
    {
        "skill": "data.weather",
        "params": {"location": "Sydney", "days": 7},
        "rationale": "a weather question needs the forecast tool",
        "done": False,
    }
)
DONE_ACTION = json.dumps({"skill": "data.weather", "params": {}, "rationale": "done", "done": True})
ANSWER = json.dumps(
    {
        "answer": "This weekend in Sydney looks overcast with mild temperatures.",
        "citations": [{"ref": "https://open-meteo.com/", "url": "https://open-meteo.com/"}],
        "confidence": 0.8,
    }
)

GEO = {
    "results": [{"name": "Sydney", "country": "Australia", "latitude": -33.87, "longitude": 151.2}]
}
FORECAST = {
    "timezone": "Australia/Sydney",
    "daily": {
        "time": ["2026-06-20", "2026-06-21"],
        "weather_code": [3, 3],
        "temperature_2m_max": [19.0, 20.0],
        "temperature_2m_min": [11.0, 12.0],
        "precipitation_probability_max": [10, 0],
    },
}


@pytest.fixture
def conn():
    c = connect()
    migrate(c)
    try:
        yield c
    finally:
        c.close()


def test_junior_researches_weather_when_memory_empty(conn, monkeypatch) -> None:
    # No memory seeded → memory.search returns nothing → research loop fires.
    monkeypatch.setattr(
        data, "_get_json", lambda url, params, *, timeout: GEO if "geocoding" in url else FORECAST
    )

    drafter = FakeProvider([ANSWER])
    providers = {"planner": FakeProvider([WEATHER_ACTION, WEATHER_ACTION]), "drafter": drafter}
    advisor = Advisor(
        resolve_provider=lambda role: providers[role], conn=conn, verify_url=lambda _u: True
    )

    req = requests_repo.create_request(conn, title="weather this weekend in Sydney")
    job = requests_repo.create_job(conn, request_id=req.id, kind="ask", complexity="simple")
    card = {
        "request_id": req.id,
        "request_code": req.code,
        "title": req.title,
        "text": "What is the weather this weekend in Sydney?",
        "append": False,
    }

    result = junior.answer_ask(conn, advisor, card, user_id=None, job_id=job.id)

    # An answer was produced (no longer a dead-end), citing the live source.
    assert result.answer is not None
    assert "Sydney" in result.answer.answer
    assert any(c.url == "https://open-meteo.com/" for c in result.answer.citations)

    # The weather tool ran exactly ONCE — the loop stops after the first useful
    # finding so it never spends a second (possibly metered) tool call, even
    # though the planner would have proposed `data.weather` again.
    skills_run = [s["skill_name"] for s in steps_repo.list_steps(conn, job.id)]
    assert "memory.search" in skills_run
    assert skills_run.count("data.weather") == 1

    # The forecast finding was fed into the drafter's prompt (so the answer is grounded).
    drafter_prompt = drafter.calls[-1].messages[0]["content"]
    assert "Weather forecast for Sydney" in drafter_prompt


def test_junior_skips_research_when_memory_has_hits(conn, monkeypatch) -> None:
    # Seed a relevant memory → memory.search has hits → research must NOT fire.
    from app.storage.repos import memories as memories_repo

    memories_repo.create_memory(conn, content="the capital of France is Paris")

    # A planner provider that would explode if next_action were called.
    def _boom(_role):
        if _role == "planner":
            raise AssertionError("research should not run when memory has hits")
        return FakeProvider([ANSWER])

    advisor = Advisor(resolve_provider=_boom, conn=conn, verify_url=lambda _u: True)

    req = requests_repo.create_request(conn, title="capital of France")
    job = requests_repo.create_job(conn, request_id=req.id, kind="ask", complexity="simple")
    card = {
        "request_id": req.id,
        "request_code": req.code,
        "title": req.title,
        "text": "capital of France?",
        "append": False,
    }

    result = junior.answer_ask(conn, advisor, card, user_id=None, job_id=job.id)
    assert result.answer is not None
    skills_run = [s["skill_name"] for s in steps_repo.list_steps(conn, job.id)]
    assert skills_run == ["memory.search"]  # no research tools ran


def test_junior_stores_research_findings_as_temporary_memories(conn, monkeypatch) -> None:
    # When research finds live data (weather, web fetch, etc.), it should be
    # stored as a short-lived memory so follow-up questions can reuse it.
    from app.storage.repos import memories as memories_repo

    monkeypatch.setattr(
        data, "_get_json", lambda url, params, *, timeout: GEO if "geocoding" in url else FORECAST
    )

    drafter = FakeProvider([ANSWER])
    providers = {"planner": FakeProvider([WEATHER_ACTION, WEATHER_ACTION]), "drafter": drafter}
    advisor = Advisor(
        resolve_provider=lambda role: providers[role], conn=conn, verify_url=lambda _u: True
    )

    req = requests_repo.create_request(conn, title="weather this weekend in Sydney")
    job = requests_repo.create_job(conn, request_id=req.id, kind="ask", complexity="simple")
    card = {
        "request_id": req.id,
        "request_code": req.code,
        "title": req.title,
        "text": "What is the weather this weekend in Sydney?",
        "append": False,
    }

    result = junior.answer_ask(conn, advisor, card, user_id=None, job_id=job.id)

    # Research ran and produced an answer.
    assert result.answer is not None
    assert "Sydney" in result.answer.answer

    # The research finding was stored as a short-lived memory with kind='research'.
    all_memories = memories_repo.search_memories(conn, "Sydney", limit=100)
    research_memories = [m for m in all_memories if m.kind == "research"]
    assert len(research_memories) > 0, "Research finding should be stored as a memory"

    # Verify the memory has the right properties (short-lived, confidence, etc).
    mem = research_memories[0]
    assert mem.retention_class == "short", "Research should be stored as short-lived"
    assert mem.importance == 0.7, "Research findings should have moderate importance"
    assert mem.confidence == 0.8, "Research should have high confidence"
    assert "Sydney" in mem.content or "Sydney" in (mem.summary or "")
    assert mem.expires_at is not None, "Research memory should have an expiry time"
    assert mem.source_ref is not None, "Research memory should have a source URL"

    # On a follow-up, the stored memory can be found instead of re-fetching.
    # Follow-up reuse marker: source ref + entity key are present for lookup/dedup.
    assert mem.source_ref == "https://open-meteo.com/"
    assert mem.entity_key.startswith("data.weather:")
