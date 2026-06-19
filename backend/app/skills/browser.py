"""Browser skills — search and read the web with a real headless browser (§8).

Two **read-only** skills that drive a headless Chromium (via Playwright) so the
agent can work with pages a plain HTTP GET can't:

* ``browser.search`` (read) — run a web search in a headless browser and return
  ranked results (title, url, snippet). **Keyless**, unlike ``web.search``
  (which needs a Tavily key).
* ``browser.fetch``  (read) — open a URL in a headless browser, let its
  JavaScript render, then return the readable text — for SPA/JS-heavy pages a
  static ``web.fetch`` returns little or nothing for.

Safety:

* Both are **read-only** — they navigate and read, never click/type/submit.
  Automated *interaction* with sites is deliberately out of scope (a much larger,
  riskier surface); add it later behind explicit gating if ever needed.
* ``browser.fetch`` is **SSRF-guarded**: the target must resolve to a public
  address, every in-page request to a private IP literal is aborted, and the
  final URL is re-checked after any in-page redirect.
* Playwright is an **optional** dependency (``pip install -e '.[browser]'`` then
  ``playwright install chromium``); when it is missing the skills return a clear
  *not available* result instead of crashing — the offline, hash-pinned core is
  never forced to carry a browser binary.

The single browser boundary is the :func:`_render_page` seam, so the whole test
suite runs offline by monkeypatching it (nothing launches a browser or touches
the network).
"""

from __future__ import annotations

import base64
import re
from dataclasses import dataclass
from html import unescape as _unescape
from urllib.parse import parse_qs, quote_plus, urlparse

from pydantic import BaseModel, Field

from app.advisor.citations import is_public_fetch_url
from app.advisor.redaction import redact_text
from app.skills.context import SkillContext
from app.skills.registry import skill
from app.skills.web import _html_title, _html_to_text  # shared HTML→text helpers

# A Chrome-like UA so sites serve the normal (not a bot-blocked) page.
_USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
)
DEFAULT_TIMEOUT = 30.0
_MAX_TEXT_CHARS = 8_000

# Bing's web search results page renders for a real headless browser (unlike
# DuckDuckGo, which serves a 403 challenge to automated/headless clients).
_BING_SEARCH = "https://www.bing.com/search"


class BrowserUnavailable(RuntimeError):
    """Raised when the optional headless-browser dependency isn't available."""


@dataclass
class _PageResult:
    """What the browser seam returns: final URL, HTTP status, and rendered HTML."""

    url: str
    status: int | None
    html: str


def _render_page(url: str, *, timeout: float = DEFAULT_TIMEOUT) -> _PageResult:
    """Seam: load ``url`` in headless Chromium and return the rendered page.

    Lazily imports Playwright so the heavy optional dependency is only needed
    when a browser skill actually runs. As defense-in-depth, every in-page
    request to a private/loopback IP **literal** is aborted (a page can't be used
    to reach internal http(s) services). Tests monkeypatch this whole function,
    so it never runs in the offline suite.
    """
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:  # optional dependency not installed
        raise BrowserUnavailable(
            "headless browser is not available — install it with "
            "`pip install -e '.[browser]'` then `playwright install chromium`"
        ) from exc

    from app.advisor.citations import is_safe_fetch_target

    def _guard(route) -> None:
        req = route.request.url
        # Only police http(s); allow data:/blob:/about: so inline resources render.
        if urlparse(req).scheme in ("http", "https") and not is_safe_fetch_target(req):
            route.abort()
        else:
            route.continue_()

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        try:
            page = browser.new_page(user_agent=_USER_AGENT)
            page.route("**/*", _guard)
            response = page.goto(url, timeout=timeout * 1000, wait_until="load")
            return _PageResult(
                url=page.url,
                status=response.status if response else None,
                html=page.content(),
            )
        finally:
            browser.close()


# --- browser.fetch ----------------------------------------------------------


class FetchParams(BaseModel):
    url: str = Field(
        ..., min_length=1, description="The http(s) URL to open in a headless browser."
    )
    max_chars: int = Field(_MAX_TEXT_CHARS, ge=200, le=50_000)


class FetchResult(BaseModel):
    ok: bool
    url: str
    status: int | None = None
    title: str | None = None
    text: str = ""
    error: str | None = None


@skill(
    name="browser.fetch",
    description=(
        "Open a public web page in a headless browser, let its JavaScript render, "
        "and return the readable text. Use for pages that need JavaScript (single-"
        "page apps, infinite scroll) where a plain fetch returns little or nothing."
    ),
    params=FetchParams,
    returns=FetchResult,
    permissions=["web.read"],
    effect="read",
)
def browser_fetch(params: FetchParams, ctx: SkillContext) -> FetchResult:
    url = params.url.strip()
    if not is_public_fetch_url(url):
        return FetchResult(ok=False, url=url, error="URL is not a public http(s) address")
    try:
        page = _render_page(url)
    except BrowserUnavailable as exc:
        return FetchResult(ok=False, url=url, error=str(exc))
    except Exception as exc:  # noqa: BLE001 - any navigation/launch failure is non-fatal
        return FetchResult(ok=False, url=url, error=f"browser failed: {exc}")
    # An in-page (JS or 3xx) redirect could land on an internal address — re-check.
    if not is_public_fetch_url(page.url):
        return FetchResult(ok=False, url=page.url, error="redirected to a non-public address")
    ok = page.status is None or page.status < 400
    return FetchResult(
        ok=ok,
        url=page.url,
        status=page.status,
        title=_html_title(page.html),
        text=_html_to_text(page.html)[: params.max_chars],
        error=None if ok else f"HTTP {page.status}",
    )


# --- browser.search (keyless, via Bing) -------------------------------------


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
    results: list[SearchHit] = Field(default_factory=list)
    error: str | None = None


# Bing search result markup: each organic result is an ``<li class="b_algo">``
# block whose ``<h2><a href>`` is the title/link (a ``/ck/a?...&u=a1<base64>``
# redirect we decode) and whose first ``<p>`` is the snippet.
_RESULT_BLOCK = re.compile(
    r'<li class="b_algo".*?(?=<li class="b_algo"|</ol>)', re.IGNORECASE | re.DOTALL
)
_RESULT_TITLE = re.compile(
    r'<h2[^>]*>\s*<a\b[^>]+href="([^"]+)"[^>]*>(.*?)</a>', re.IGNORECASE | re.DOTALL
)
_RESULT_SNIPPET = re.compile(r"<p\b[^>]*>(.*?)</p>", re.IGNORECASE | re.DOTALL)


def _decode_bing_href(href: str) -> str:
    """Unwrap Bing's ``/ck/a?...&u=a1<base64url>`` redirect into the real URL."""
    href = _unescape(href.strip())
    parts = urlparse(href)
    if not (parts.netloc.endswith("bing.com") and parts.path.startswith("/ck/a")):
        return href
    token = parse_qs(parts.query).get("u", [""])[0]
    if not token.startswith("a1"):
        return href
    raw = token[2:].replace("+", "-").replace("/", "_")  # tolerate either base64 alphabet
    try:
        target = base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4)).decode("utf-8", "replace")
    except (ValueError, UnicodeDecodeError):
        return href
    return target if target.startswith(("http://", "https://")) else href


def _parse_bing_results(html: str, max_results: int) -> list[SearchHit]:
    """Parse Bing search results into ranked hits (pure; offline-testable)."""
    hits: list[SearchHit] = []
    for block in _RESULT_BLOCK.findall(html):
        if len(hits) >= max_results:
            break
        title = _RESULT_TITLE.search(block)
        if not title:
            continue
        snippet = _RESULT_SNIPPET.search(block)
        hits.append(
            SearchHit(
                title=_html_to_text(title.group(2)),
                url=_decode_bing_href(title.group(1)),
                snippet=_html_to_text(snippet.group(1)) if snippet else "",
            )
        )
    return hits


@skill(
    name="browser.search",
    description=(
        "Search the public web with a headless browser and return ranked results "
        "(title, url, snippet). A keyless alternative to web.search; use it to find "
        "pages when no search API key is set, then read one with browser.fetch."
    ),
    params=SearchParams,
    returns=SearchResult,
    permissions=["web.read"],
    effect="read",
)
def browser_search(params: SearchParams, ctx: SkillContext) -> SearchResult:
    # Scrub any secret-looking content from the query before it leaves the machine.
    query = redact_text(params.query).strip()
    search_url = f"{_BING_SEARCH}?q={quote_plus(query)}"
    try:
        page = _render_page(search_url)
    except BrowserUnavailable as exc:
        return SearchResult(ok=False, query=query, error=str(exc))
    except Exception as exc:  # noqa: BLE001 - any navigation/launch failure is non-fatal
        return SearchResult(ok=False, query=query, error=f"browser failed: {exc}")
    results = _parse_bing_results(page.html, params.max_results)
    if not results:
        return SearchResult(
            ok=False,
            query=query,
            error="no results found (the search page may have blocked automated access)",
        )
    return SearchResult(ok=True, query=query, results=results)
