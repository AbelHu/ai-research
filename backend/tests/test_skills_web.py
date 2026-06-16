"""Tests for the web skills — `web.fetch` and `web.search` (design-spec §8).

Offline: the SSRF guard rejects private/loopback/non-http targets *statically*
(no DNS), and the network seams (`_http_get` / `_tavily_search`) are monkeypatched
so nothing touches the network.
"""

from __future__ import annotations

import httpx
import pytest

from app.skills import web
from app.skills.context import SkillContext
from app.skills.web import FetchParams, SearchParams, web_fetch, web_search
from app.storage.db import connect
from app.storage.migrations import migrate


@pytest.fixture
def ctx():
    conn = connect()
    migrate(conn)
    try:
        yield SkillContext(user_id=0, conn=conn, permissions=frozenset({"web.read"}))
    finally:
        conn.close()


@pytest.fixture(autouse=True)
def _clear_search_cache():
    # The web.search result cache is module-level; reset it around every test so
    # cases don't leak cached results into one another.
    web.clear_search_cache()
    yield
    web.clear_search_cache()


def _response(
    status: int, *, text: str = "", headers: dict | None = None, url: str
) -> httpx.Response:
    return httpx.Response(
        status, text=text, headers=headers or {}, request=httpx.Request("GET", url)
    )


# --- web.fetch SSRF guard (static, no DNS) ----------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1/admin",  # loopback IP literal
        "http://169.254.169.254/latest/meta-data/",  # cloud metadata
        "http://10.0.0.5/internal",  # private IP literal
        "file:///etc/passwd",  # non-http scheme
        "ftp://example.com/x",  # non-http scheme
    ],
)
def test_web_fetch_blocks_unsafe_urls(ctx, url) -> None:
    result = web_fetch(FetchParams(url=url), ctx)
    assert result.ok is False
    assert "public" in (result.error or "")


def test_web_fetch_returns_readable_text(ctx, monkeypatch) -> None:
    monkeypatch.setattr(web, "is_public_fetch_url", lambda _u: True)
    html = (
        "<html><head><title>Hi &amp; Bye</title></head><body><p>Hello <b>world</b></p>"
        "<script>ignore()</script></body></html>"
    )
    monkeypatch.setattr(
        web,
        "_http_get",
        lambda url, *, timeout: _response(
            200, text=html, headers={"content-type": "text/html"}, url=url
        ),
    )
    result = web_fetch(FetchParams(url="https://example.com/page"), ctx)
    assert result.ok is True
    assert result.title == "Hi & Bye"
    assert "Hello world" in result.text
    assert "ignore()" not in result.text  # script stripped


def test_web_fetch_follows_redirect_and_revalidates(ctx, monkeypatch) -> None:
    monkeypatch.setattr(web, "is_public_fetch_url", lambda _u: True)
    seen: list[str] = []

    def fake_get(url, *, timeout):
        seen.append(url)
        if url == "https://example.com/start":
            return _response(302, headers={"location": "https://example.com/final"}, url=url)
        return _response(200, text="done", headers={"content-type": "text/plain"}, url=url)

    monkeypatch.setattr(web, "_http_get", fake_get)
    result = web_fetch(FetchParams(url="https://example.com/start"), ctx)
    assert result.ok is True
    assert result.text == "done"
    assert seen == ["https://example.com/start", "https://example.com/final"]


# --- web.search (Tavily, env-gated) -----------------------------------------


def test_web_search_not_configured_without_key(ctx, monkeypatch) -> None:
    monkeypatch.setattr(web, "_resolve_tavily_key", lambda _ctx: None)
    result = web_search(SearchParams(query="latest ai news"), ctx)
    assert result.ok is False
    assert "TAVILY_API_KEY" in (result.error or "")
    assert result.results == []


def test_web_search_returns_normalized_results(ctx, monkeypatch) -> None:
    monkeypatch.setattr(web, "_resolve_tavily_key", lambda _ctx: "tvly-key")
    captured: dict = {}

    def fake_tavily(key, query, max_results, *, timeout):
        captured.update(key=key, query=query, max_results=max_results)
        return {
            "answer": "Sydney will be overcast.",
            "results": [
                {"title": "BOM Sydney", "url": "https://bom.gov.au/syd", "content": "overcast 19C"},
                {"title": "Weather.com", "url": "https://weather.com/syd", "content": "cloudy"},
            ],
        }

    monkeypatch.setattr(web, "_tavily_search", fake_tavily)
    result = web_search(SearchParams(query="weather in Sydney", max_results=2), ctx)

    assert result.ok is True
    assert result.answer == "Sydney will be overcast."
    assert [h.url for h in result.results] == ["https://bom.gov.au/syd", "https://weather.com/syd"]
    assert captured["key"] == "tvly-key"
    assert captured["max_results"] == 2


def test_web_search_redacts_secret_in_query(ctx, monkeypatch) -> None:
    monkeypatch.setattr(web, "_resolve_tavily_key", lambda _ctx: "tvly-key")
    sent: dict = {}
    monkeypatch.setattr(
        web,
        "_tavily_search",
        lambda key, query, max_results, *, timeout: sent.update(query=query) or {"results": []},
    )
    planted = "ghp_0123456789abcdefghijklmnopqrstuvwxyz12"
    web_search(SearchParams(query=f"look up my token {planted}"), ctx)
    assert planted not in sent["query"]
    assert "[REDACTED]" in sent["query"]


# --- credit-conservation safeguards -----------------------------------------


def _fake_policy(*, daily_max=50, cache_ttl_minutes=15):
    from types import SimpleNamespace

    return SimpleNamespace(
        web_search_daily_max=daily_max, web_search_cache_ttl_minutes=cache_ttl_minutes
    )


def test_web_search_caches_identical_query(ctx, monkeypatch) -> None:
    # An identical query within the TTL must be served from cache (0 credits).
    monkeypatch.setattr(web, "_resolve_tavily_key", lambda _ctx: "tvly-key")
    monkeypatch.setattr(web, "_search_policy", _fake_policy)
    calls = {"n": 0}

    def fake_tavily(key, query, max_results, *, timeout):
        calls["n"] += 1
        return {"results": [{"title": "T", "url": "https://x/1", "content": "c"}]}

    monkeypatch.setattr(web, "_tavily_search", fake_tavily)

    first = web_search(SearchParams(query="weather in Perth"), ctx)
    second = web_search(SearchParams(query="weather in Perth"), ctx)

    assert first.ok is True and first.cached is False
    assert second.ok is True and second.cached is True  # served from cache
    assert calls["n"] == 1  # the API was hit only once
    # And only one call was charged against the daily budget.
    from app.storage.repos import api_usage as api_usage_repo

    assert api_usage_repo.count_today(ctx.conn, "tavily") == 1


def test_web_search_enforces_daily_budget(ctx, monkeypatch) -> None:
    # With the cap at 2, the 3rd distinct query is refused without hitting the API.
    monkeypatch.setattr(web, "_resolve_tavily_key", lambda _ctx: "tvly-key")
    monkeypatch.setattr(web, "_search_policy", lambda: _fake_policy(daily_max=2))
    calls = {"n": 0}

    def fake_tavily(key, query, max_results, *, timeout):
        calls["n"] += 1
        return {"results": [{"title": "T", "url": "https://x/q", "content": "c"}]}

    monkeypatch.setattr(web, "_tavily_search", fake_tavily)

    assert web_search(SearchParams(query="q1"), ctx).ok is True
    assert web_search(SearchParams(query="q2"), ctx).ok is True
    blocked = web_search(SearchParams(query="q3"), ctx)

    assert blocked.ok is False
    assert "budget" in (blocked.error or "")
    assert calls["n"] == 2  # the over-budget call never reached the API
