"""Web skills — fetch a URL or search the web (design-spec §8; web access).

* ``web.fetch``  (read) — GET a **public** URL and return its readable text.
  SSRF-guarded (scheme + DNS public-IP check, redirect re-validation).
* ``web.search`` (read) — search the web via a managed provider (**Tavily**).
  Off until ``TAVILY_API_KEY`` is set; returns a clear *not configured* result
  otherwise, so the answer path degrades honestly instead of crashing.

Both leave the machine over **https/http only**, never write, and never touch a
model (skills are model-independent, §8.5). The outbound search query is
secret-scrubbed (O16) as defense in depth. The HTTP calls go through small
module-level seams (``_http_get`` / ``_tavily_search``) so tests run fully
offline by monkeypatching them.
"""

from __future__ import annotations

import html as _html
import re
import time

import httpx
from pydantic import BaseModel, Field

from app.advisor.citations import is_public_fetch_url
from app.advisor.redaction import redact_text
from app.config.settings import Settings, get_settings
from app.skills.context import SkillContext
from app.skills.registry import skill
from app.storage.repos import api_usage as api_usage_repo

# A Chrome-like UA — some sites refuse the default httpx agent.
_USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
)
DEFAULT_TIMEOUT = 20.0
_MAX_TEXT_CHARS = 8_000
_MAX_REDIRECTS = 3

# Tavily managed search endpoint (free 1,000/mo, no card).
TAVILY_ENDPOINT = "https://api.tavily.com/search"
# Usage-meter provider key for the daily budget cap (api_usage table).
_SEARCH_PROVIDER = "tavily"
# In-process result cache so identical queries within the TTL window cost no
# credits (key -> (monotonic_ts, SearchResult)). Shared across threads in the
# one service process; a plain dict is fine for a best-effort cache.
_SEARCH_CACHE: dict[str, tuple[float, SearchResult]] = {}

_SCRIPT_STYLE = re.compile(r"<(script|style)\b[^>]*>.*?</\1>", re.IGNORECASE | re.DOTALL)
_TAG = re.compile(r"<[^>]+>")
_TITLE = re.compile(r"<title[^>]*>(.*?)</title>", re.IGNORECASE | re.DOTALL)
_WS = re.compile(r"\s+")


def _html_to_text(body: str) -> str:
    """Strip scripts/styles/tags and collapse whitespace into readable text."""
    body = _SCRIPT_STYLE.sub(" ", body)
    body = _TAG.sub(" ", body)
    return _WS.sub(" ", _html.unescape(body)).strip()


def _html_title(body: str) -> str | None:
    match = _TITLE.search(body)
    return _WS.sub(" ", _html.unescape(match.group(1))).strip() if match else None


# --- web.fetch --------------------------------------------------------------


class FetchParams(BaseModel):
    url: str = Field(..., min_length=1, description="The http(s) URL to fetch.")
    max_chars: int = Field(_MAX_TEXT_CHARS, ge=200, le=50_000)


class FetchResult(BaseModel):
    ok: bool
    url: str
    status: int | None = None
    title: str | None = None
    text: str = ""
    error: str | None = None


def _http_get(url: str, *, timeout: float) -> httpx.Response:
    """Seam: a single non-redirecting GET (tests monkeypatch this)."""
    return httpx.get(
        url,
        timeout=timeout,
        follow_redirects=False,
        headers={"User-Agent": _USER_AGENT, "Accept-Language": "en"},
    )


@skill(
    name="web.fetch",
    description=(
        "Fetch a public web page or API URL and return its readable text. "
        "Use when you already have a specific URL to read."
    ),
    params=FetchParams,
    returns=FetchResult,
    permissions=["web.read"],
    effect="read",
)
def web_fetch(params: FetchParams, ctx: SkillContext) -> FetchResult:
    url = params.url.strip()
    # Follow redirects manually, re-validating SSRF safety at every hop (a server
    # can 3xx a public URL toward an internal address).
    for _ in range(_MAX_REDIRECTS + 1):
        if not is_public_fetch_url(url):
            return FetchResult(ok=False, url=url, error="URL is not a public http(s) address")
        try:
            resp = _http_get(url, timeout=DEFAULT_TIMEOUT)
        except httpx.HTTPError as exc:
            return FetchResult(ok=False, url=url, error=f"fetch failed: {exc}")
        if resp.is_redirect:
            location = resp.headers.get("location")
            if not location:
                break
            url = str(httpx.URL(url).join(location))
            continue
        body = resp.text
        content_type = resp.headers.get("content-type", "")
        if "html" in content_type:
            text = _html_to_text(body)
            title = _html_title(body)
        else:
            text = body.strip()
            title = None
        ok = resp.status_code < 400
        return FetchResult(
            ok=ok,
            url=str(resp.url) or url,
            status=resp.status_code,
            title=title,
            text=text[: params.max_chars],
            error=None if ok else f"HTTP {resp.status_code}",
        )
    return FetchResult(ok=False, url=url, error="too many redirects")


# --- web.search (Tavily) ----------------------------------------------------


class SearchParams(BaseModel):
    query: str = Field(..., min_length=1, description="What to search the web for.")
    max_results: int = Field(5, ge=1, le=10)


class SearchHit(BaseModel):
    title: str = ""
    url: str = ""
    snippet: str = ""


class SearchResult(BaseModel):
    ok: bool
    query: str
    answer: str | None = None
    results: list[SearchHit] = Field(default_factory=list)
    cached: bool = False  # served from the in-process cache (no credit spent)
    error: str | None = None


def _resolve_tavily_key(ctx: SkillContext) -> str | None:
    settings: Settings = ctx.config or get_settings()
    key = settings.tavily_api_key
    return key.reveal() if key is not None else None


def _search_policy():
    """The web-search budget knobs (indirection so tests can override)."""
    from app.config.policies import get_policies

    return get_policies()


def _cache_key(query: str, max_results: int) -> str:
    return f"{max_results}:{query.strip().lower()}"


def clear_search_cache() -> None:
    """Drop all cached search results (used by tests + on demand)."""
    _SEARCH_CACHE.clear()


def _cache_get(key: str, ttl_seconds: float) -> SearchResult | None:
    entry = _SEARCH_CACHE.get(key)
    if entry is None:
        return None
    ts, result = entry
    if ttl_seconds > 0 and (time.monotonic() - ts) > ttl_seconds:
        _SEARCH_CACHE.pop(key, None)
        return None
    return result


def _tavily_search(key: str, query: str, max_results: int, *, timeout: float) -> dict:
    """Seam: POST the Tavily search API (tests monkeypatch this)."""
    resp = httpx.post(
        TAVILY_ENDPOINT,
        timeout=timeout,
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        json={"query": query, "max_results": max_results},
    )
    resp.raise_for_status()
    return resp.json()


@skill(
    name="web.search",
    description=(
        "Search the public web for current information. Returns ranked results "
        "(title, url, snippet) and a short answer when available. Use for general "
        "or up-to-date questions when you don't already have a specific URL."
    ),
    params=SearchParams,
    returns=SearchResult,
    permissions=["web.read"],
    effect="read",
)
def web_search(params: SearchParams, ctx: SkillContext) -> SearchResult:
    # Scrub any secret-looking content from the query before it leaves the machine.
    query = redact_text(params.query)
    key = _resolve_tavily_key(ctx)
    if not key:
        return SearchResult(
            ok=False,
            query=query,
            error="web search is not configured (set TAVILY_API_KEY in .env)",
        )

    policy = _search_policy()

    # 1) Cache: an identical recent query is served free (no credit spent).
    cache_key = _cache_key(query, params.max_results)
    cached = _cache_get(cache_key, policy.web_search_cache_ttl_minutes * 60)
    if cached is not None:
        return cached.model_copy(update={"cached": True})

    # 2) Daily budget cap: never exceed the metered free quota (credit guard).
    used = api_usage_repo.count_today(ctx.conn, _SEARCH_PROVIDER)
    if policy.web_search_daily_max and used >= policy.web_search_daily_max:
        return SearchResult(
            ok=False,
            query=query,
            error=(
                f"daily web-search budget reached ({used}/{policy.web_search_daily_max}); "
                "conserving credits"
            ),
        )

    try:
        data = _tavily_search(key, query, params.max_results, timeout=DEFAULT_TIMEOUT)
    except httpx.HTTPError as exc:
        return SearchResult(ok=False, query=query, error=f"search failed: {exc}")

    # Count this real call against today's budget (cached/failed calls don't).
    api_usage_repo.increment(ctx.conn, _SEARCH_PROVIDER)

    results = [
        SearchHit(
            title=str(r.get("title") or ""),
            url=str(r.get("url") or ""),
            snippet=str(r.get("content") or ""),
        )
        for r in data.get("results", [])
        if isinstance(r, dict)
    ]
    answer = data.get("answer")
    result = SearchResult(ok=True, query=query, answer=answer or None, results=results)
    _SEARCH_CACHE[cache_key] = (time.monotonic(), result)
    return result
