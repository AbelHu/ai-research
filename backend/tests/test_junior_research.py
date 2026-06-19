"""Test the Junior's research loop: gather web/live context when memory is empty.

Offline: the advisor (per-role `FakeProvider`) proposes `data.weather`, whose
Open-Meteo seam is monkeypatched. Proves an unanswerable-from-memory ask (e.g.
weather) now gets answered + cited from a read tool instead of dead-ending.
"""

from __future__ import annotations

import base64
import json

import pytest

from app.advisor.wrapper import Advisor
from app.roles import junior
from app.skills import browser as browser_skill
from app.skills import data
from app.skills import web as web_skill
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


# --- browser-first / web-fallback search fixtures ---------------------------


def _bing_redirect(target: str) -> str:
    enc = base64.urlsafe_b64encode(target.encode()).decode().rstrip("=")
    return f"https://www.bing.com/ck/a?!&amp;u=a1{enc}&amp;ntb=1"


_RESTAURANTS_URL = "https://www.goodfood.com.au/sydney"
_BING_HTML = (
    '<ol id="b_results"><li class="b_algo">'
    f'<h2><a href="{_bing_redirect(_RESTAURANTS_URL)}">Best Restaurants in Sydney</a></h2>'
    "<p>Top Sydney picks: Quay, Bennelong, and Tetsuya's.</p>"
    "</li></ol>"
)
_BING_EMPTY = "<html><body>no results</body></html>"

SEARCH_ACTION = json.dumps(
    {
        "skill": "browser.search",
        "params": {"query": "best restaurants in Sydney", "max_results": 5},
        "rationale": "find current restaurant recommendations",
        "done": False,
    }
)
RESTAURANT_ANSWER = json.dumps(
    {
        "answer": "Top Sydney restaurants include Quay and Bennelong.",
        "citations": [{"ref": _RESTAURANTS_URL, "url": _RESTAURANTS_URL}],
        "confidence": 0.8,
    }
)


def _render_html(html: str):
    return lambda _url, **_k: browser_skill._PageResult(
        url="https://www.bing.com/search", status=200, html=html
    )


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


def _research_card(conn):
    req = requests_repo.create_request(conn, title="best restaurants in Sydney")
    job = requests_repo.create_job(conn, request_id=req.id, kind="ask", complexity="simple")
    card = {
        "request_id": req.id,
        "request_code": req.code,
        "title": req.title,
        "text": "best restaurants in Sydney?",
        "append": False,
        "domain": "research",
    }
    return req, job, card


def test_research_search_uses_browser_first(conn, monkeypatch) -> None:
    # The headless browser finds results → it is used; web.search must NOT run.
    monkeypatch.setattr(browser_skill, "_render_page", _render_html(_BING_HTML))

    def _no_tavily(*_a, **_k):
        raise AssertionError("web.search must not run when the browser found results")

    monkeypatch.setattr(web_skill, "_tavily_search", _no_tavily)

    drafter = FakeProvider([RESTAURANT_ANSWER])
    providers = {"planner": FakeProvider([SEARCH_ACTION]), "drafter": drafter}
    advisor = Advisor(
        resolve_provider=lambda r: providers[r], conn=conn, verify_url=lambda _u: True
    )
    _req, job, card = _research_card(conn)

    result = junior.answer_ask(conn, advisor, card, user_id=None, job_id=job.id)

    assert result.answer is not None
    skills = [s["skill_name"] for s in steps_repo.list_steps(conn, job.id)]
    assert "browser.search" in skills
    assert "web.search" not in skills  # browser succeeded → no metered fallback
    assert "Best Restaurants in Sydney" in drafter.calls[-1].messages[0]["content"]


def test_research_search_falls_back_to_web(conn, monkeypatch) -> None:
    # The browser finds nothing → web.search (Tavily) is the fallback.
    monkeypatch.setattr(
        junior,
        "_research_tool_names",
        lambda: {"browser.search", "web.search", "data.weather", "web.fetch"},
    )
    monkeypatch.setattr(browser_skill, "_render_page", _render_html(_BING_EMPTY))
    web_skill.clear_search_cache()
    monkeypatch.setattr(web_skill, "_resolve_tavily_key", lambda _ctx: "test-key")
    monkeypatch.setattr(
        web_skill,
        "_tavily_search",
        lambda key, q, n, *, timeout: {
            "answer": "Sydney's best include Quay and Bennelong.",
            "results": [
                {"title": "Good Food Sydney", "url": _RESTAURANTS_URL, "content": "Quay, Bennelong"}
            ],
        },
    )

    drafter = FakeProvider([RESTAURANT_ANSWER])
    providers = {"planner": FakeProvider([SEARCH_ACTION]), "drafter": drafter}
    advisor = Advisor(
        resolve_provider=lambda r: providers[r], conn=conn, verify_url=lambda _u: True
    )
    _req, job, card = _research_card(conn)

    result = junior.answer_ask(conn, advisor, card, user_id=None, job_id=job.id)

    assert result.answer is not None
    skills = [s["skill_name"] for s in steps_repo.list_steps(conn, job.id)]
    assert "browser.search" in skills  # tried first
    assert "web.search" in skills  # fell back when the browser was empty
    assert skills.index("browser.search") < skills.index("web.search")


def test_research_domain_searches_despite_memory_hits(conn, monkeypatch) -> None:
    # A research ask looks outward even when memory has a (matching) hit, so a
    # stale cached memory can't preempt a fresh lookup. The browser finding — not
    # the memory — grounds the answer.
    from app.storage.repos import memories as memories_repo

    memories_repo.create_memory(conn, content="Sydney has many restaurants.", kind="research")
    monkeypatch.setattr(browser_skill, "_render_page", _render_html(_BING_HTML))

    drafter = FakeProvider([RESTAURANT_ANSWER])
    providers = {"planner": FakeProvider([SEARCH_ACTION]), "drafter": drafter}
    advisor = Advisor(
        resolve_provider=lambda r: providers[r], conn=conn, verify_url=lambda _u: True
    )
    _req, job, card = _research_card(conn)

    result = junior.answer_ask(conn, advisor, card, user_id=None, job_id=job.id)

    assert result.answer is not None
    skills = [s["skill_name"] for s in steps_repo.list_steps(conn, job.id)]
    assert "memory.search" in skills and "browser.search" in skills
    prompt = drafter.calls[-1].messages[0]["content"]
    assert "Best Restaurants in Sydney" in prompt  # fresh browser finding leads
    assert "has many restaurants" not in prompt  # stale memory was not used


# A research-loop "done" action so the loop sends its prompt then stops without
# actually running a tool (keeps the catalog-gating assertions offline).
RESEARCH_DONE = json.dumps(
    {"skill": "data.weather", "params": {}, "rationale": "done", "done": True}
)


def test_coding_ask_excludes_web_tools_from_research(conn, monkeypatch) -> None:
    # Simulate Tavily configured so web.search would normally be offered.
    monkeypatch.setattr(
        junior, "_research_tool_names", lambda: {"web.search", "web.fetch", "data.weather"}
    )
    planner = FakeProvider([RESEARCH_DONE])
    providers = {"planner": planner, "drafter": FakeProvider([ANSWER])}
    advisor = Advisor(
        resolve_provider=lambda role: providers[role], conn=conn, verify_url=lambda _u: True
    )

    req = requests_repo.create_request(conn, title="refactor my function")
    job = requests_repo.create_job(conn, request_id=req.id, kind="ask", complexity="simple")
    card = {
        "request_id": req.id,
        "request_code": req.code,
        "title": req.title,
        "text": "Refactor this Python function to be cleaner.",
        "append": False,
        "domain": "coding",
    }

    junior.answer_ask(conn, advisor, card, user_id=None, job_id=job.id)

    # The research catalog shown to the planner excludes web tools for coding.
    planner_prompt = planner.calls[-1].messages[0]["content"]
    assert "web.search" not in planner_prompt
    assert "web.fetch" not in planner_prompt
    assert "data.weather" in planner_prompt  # non-web research tool still offered


def test_general_ask_offers_web_tools_in_research(conn, monkeypatch) -> None:
    monkeypatch.setattr(
        junior, "_research_tool_names", lambda: {"web.search", "web.fetch", "data.weather"}
    )
    planner = FakeProvider([RESEARCH_DONE])
    providers = {"planner": planner, "drafter": FakeProvider([ANSWER])}
    advisor = Advisor(
        resolve_provider=lambda role: providers[role], conn=conn, verify_url=lambda _u: True
    )

    req = requests_repo.create_request(conn, title="latest news on vendors")
    job = requests_repo.create_job(conn, request_id=req.id, kind="ask", complexity="simple")
    card = {
        "request_id": req.id,
        "request_code": req.code,
        "title": req.title,
        "text": "What's the latest pricing for these vendors?",
        "append": False,
        "domain": "general",
    }

    junior.answer_ask(conn, advisor, card, user_id=None, job_id=job.id)

    planner_prompt = planner.calls[-1].messages[0]["content"]
    assert "web.search" in planner_prompt

