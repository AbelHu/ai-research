"""Tests for the browser skills — `browser.search` and `browser.fetch` (§8).

Fully offline: the single browser boundary (`_render_page`) is monkeypatched so
no Chromium launches and nothing touches the network, and the SSRF guard rejects
private/loopback/non-http targets statically (no DNS). Playwright need not be
installed for these tests.
"""

from __future__ import annotations

import base64

import pytest

from app.skills import browser, toolpolicy
from app.skills.browser import (
    BrowserUnavailable,
    FetchParams,
    SearchParams,
    _decode_bing_href,
    _PageResult,
    _parse_bing_results,
    browser_fetch,
    browser_search,
)
from app.skills.context import SkillContext
from app.skills.registry import REGISTRY
from app.storage.db import connect
from app.storage.migrations import migrate


def _bing_redirect(target: str) -> str:
    """Build a Bing ``/ck/a?...&u=a1<base64url>`` redirect link like Bing emits."""
    enc = base64.urlsafe_b64encode(target.encode()).decode().rstrip("=")
    return f"https://www.bing.com/ck/a?!&amp;&amp;p=abc&amp;u=a1{enc}&amp;ntb=1"


_PY_LINK = _bing_redirect("https://www.python.org/")
_SAMPLE_BING_HTML = f"""
<ol id="b_results">
<li class="b_algo">
<h2><a href="{_PY_LINK}">Welcome to the <strong>Python</strong> website</a></h2>
<div class="b_caption"><p>The official home of the <strong>Python</strong> language.</p></div>
</li>
<li class="b_algo">
<h2><a href="https://en.wikipedia.org/wiki/Python">Python (programming language)</a></h2>
<p>Python is a high-level, general-purpose programming language.</p>
</li>
</ol>
"""


@pytest.fixture
def ctx():
    conn = connect()
    migrate(conn)
    try:
        yield SkillContext(user_id=0, conn=conn, permissions=frozenset({"web.read"}))
    finally:
        conn.close()


# --- registration / policy --------------------------------------------------


def test_browser_skills_are_registered() -> None:
    assert "browser.fetch" in REGISTRY
    assert "browser.search" in REGISTRY


def test_browser_skills_are_external_research_tools() -> None:
    # Available to research/general domains, excluded from coding (like web.*).
    assert {"browser.search", "browser.fetch"} <= toolpolicy.EXTERNAL_RESEARCH_SKILLS
    coding = toolpolicy.allowed_skills(domain="coding")
    assert "browser.fetch" not in coding and "browser.search" not in coding
    research = toolpolicy.allowed_skills(domain="research")
    assert "browser.fetch" in research and "browser.search" in research


# --- browser.fetch SSRF guard (static, no DNS) ------------------------------


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
def test_browser_fetch_blocks_unsafe_urls(ctx, url) -> None:
    result = browser_fetch(FetchParams(url=url), ctx)
    assert result.ok is False
    assert "public" in (result.error or "")


def test_browser_fetch_renders_text(ctx, monkeypatch) -> None:
    monkeypatch.setattr(browser, "is_public_fetch_url", lambda _u: True)
    monkeypatch.setattr(
        browser,
        "_render_page",
        lambda _url, **_k: _PageResult(
            url="https://spa.example.com/app",
            status=200,
            html=(
                "<html><head><title>SPA &amp; Co</title></head>"
                "<body><div id=app>Hello <b>rendered</b> world</div>"
                "<script>boot()</script></body></html>"
            ),
        ),
    )

    result = browser_fetch(FetchParams(url="https://spa.example.com/app"), ctx)
    assert result.ok is True
    assert result.status == 200
    assert result.title == "SPA & Co"
    assert "Hello rendered world" in result.text


def test_browser_fetch_rechecks_final_url_after_redirect(ctx, monkeypatch) -> None:
    # Input is public, but the page redirects (in-JS) to a private host: refuse it.
    monkeypatch.setattr(browser, "is_public_fetch_url", lambda u: "internal" not in u)
    monkeypatch.setattr(
        browser,
        "_render_page",
        lambda _url, **_k: _PageResult(
            url="http://internal.svc/secret", status=200, html="<html/>"
        ),
    )

    result = browser_fetch(FetchParams(url="https://public.example.com/start"), ctx)
    assert result.ok is False
    assert "non-public" in (result.error or "")


def test_browser_fetch_reports_unavailable_browser(ctx, monkeypatch) -> None:
    monkeypatch.setattr(browser, "is_public_fetch_url", lambda _u: True)

    def _boom(_url, **_k):
        raise BrowserUnavailable("headless browser is not available — install it with ...")

    monkeypatch.setattr(browser, "_render_page", _boom)

    result = browser_fetch(FetchParams(url="https://example.com"), ctx)
    assert result.ok is False
    assert "not available" in (result.error or "")


def test_browser_fetch_handles_navigation_error(ctx, monkeypatch) -> None:
    monkeypatch.setattr(browser, "is_public_fetch_url", lambda _u: True)

    def _boom(_url, **_k):
        raise RuntimeError("net::ERR_TIMED_OUT")

    monkeypatch.setattr(browser, "_render_page", _boom)

    result = browser_fetch(FetchParams(url="https://example.com"), ctx)
    assert result.ok is False
    assert "browser failed" in (result.error or "")


# --- browser.search ---------------------------------------------------------


def test_browser_search_parses_results(ctx, monkeypatch) -> None:
    monkeypatch.setattr(
        browser,
        "_render_page",
        lambda _url, **_k: _PageResult(
            url="https://www.bing.com/search", status=200, html=_SAMPLE_BING_HTML
        ),
    )

    result = browser_search(SearchParams(query="python", max_results=5), ctx)
    assert result.ok is True
    assert len(result.results) == 2
    first = result.results[0]
    assert first.url == "https://www.python.org/"  # Bing /ck/a redirect decoded
    assert first.title == "Welcome to the Python website"
    assert "official home" in first.snippet.lower()
    assert result.results[1].url == "https://en.wikipedia.org/wiki/Python"


def test_browser_search_respects_max_results(ctx, monkeypatch) -> None:
    monkeypatch.setattr(
        browser,
        "_render_page",
        lambda _url, **_k: _PageResult(url="x", status=200, html=_SAMPLE_BING_HTML),
    )
    result = browser_search(SearchParams(query="q", max_results=1), ctx)
    assert result.ok is True
    assert len(result.results) == 1


def test_browser_search_no_results_is_not_ok(ctx, monkeypatch) -> None:
    monkeypatch.setattr(
        browser,
        "_render_page",
        lambda _url, **_k: _PageResult(
            url="x", status=200, html="<html><body>no results</body></html>"
        ),
    )
    result = browser_search(SearchParams(query="zzz"), ctx)
    assert result.ok is False
    assert "no results" in (result.error or "")


def test_browser_search_reports_unavailable_browser(ctx, monkeypatch) -> None:
    def _boom(_url, **_k):
        raise BrowserUnavailable("headless browser is not available — install it with ...")

    monkeypatch.setattr(browser, "_render_page", _boom)
    result = browser_search(SearchParams(query="q"), ctx)
    assert result.ok is False
    assert "not available" in (result.error or "")


# --- pure parser helpers ----------------------------------------------------


def test_parse_bing_results_decodes_redirects_and_pairs_snippets() -> None:
    hits = _parse_bing_results(_SAMPLE_BING_HTML, max_results=5)
    assert [h.url for h in hits] == [
        "https://www.python.org/",
        "https://en.wikipedia.org/wiki/Python",
    ]
    assert hits[0].title == "Welcome to the Python website"


def test_decode_bing_href_unwraps_redirect() -> None:
    target = "https://example.org/a?x=1&y=2"
    assert _decode_bing_href(_bing_redirect(target)) == target
    # A direct (non-Bing) link is returned unchanged.
    assert _decode_bing_href("https://plain.example.com/p") == "https://plain.example.com/p"
