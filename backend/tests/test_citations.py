"""Tests for citation URL verification + SSRF guard (design-spec §6D, §7.1).

All offline: the SSRF pre-check works on schemes + IP literals without DNS, and
URL existence is exercised through an injected fake verifier — never the network.
"""

from __future__ import annotations

import httpx
import pytest

from app.advisor import citations
from app.advisor.citations import is_safe_fetch_target, unresolved_citation_urls, verify_url_exists
from app.advisor.schemas import Source


@pytest.mark.parametrize(
    "url",
    [
        "https://example.com/page",
        "http://example.com",
        "https://sub.domain.example.org/a/b?c=d",
        "https://93.184.216.34/",  # public IP literal
    ],
)
def test_safe_targets_allowed(url: str) -> None:
    assert is_safe_fetch_target(url) is True


@pytest.mark.parametrize(
    "url",
    [
        "ftp://example.com/file",  # wrong scheme
        "file:///etc/passwd",  # wrong scheme
        "javascript:alert(1)",  # wrong scheme
        "http://127.0.0.1/",  # loopback
        "http://localhost",  # loopback name resolves locally -> blocked literal? name passes static
        "http://10.0.0.1/admin",  # private
        "http://192.168.1.1/",  # private
        "http://169.254.169.254/latest/meta-data/",  # cloud metadata (link-local)
        "http://[::1]:8080/",  # IPv6 loopback
        "https://",  # no host
    ],
)
def test_unsafe_targets_blocked(url: str) -> None:
    # `localhost` is a name (not a literal) so the *static* check can't see it;
    # it is caught by DNS resolution at fetch time. Every other case is blocked
    # statically. We assert the literal/scheme/no-host cases here.
    if url == "http://localhost":
        # name passes the static pre-check; DNS-time guard handles it.
        assert is_safe_fetch_target(url) is True
    else:
        assert is_safe_fetch_target(url) is False


def test_unresolved_urls_uses_injected_verifier() -> None:
    citations = [
        Source(ref="memory:1"),  # no url -> skipped
        Source(ref="web:1", url="https://real.example/ok"),
        Source(ref="web:2", url="https://fake.example/nope"),
    ]
    existing = {"https://real.example/ok"}
    bad = unresolved_citation_urls(citations, verify=lambda u: u in existing)
    assert bad == ["https://fake.example/nope"]


def test_unresolved_urls_empty_when_all_exist() -> None:
    citations = [Source(ref="web:1", url="https://a.example/x")]
    assert unresolved_citation_urls(citations, verify=lambda u: True) == []


def test_citations_without_urls_never_call_verifier() -> None:
    calls: list[str] = []

    def _spy(url: str) -> bool:
        calls.append(url)
        return True

    unresolved_citation_urls([Source(ref="memory:9")], verify=_spy)
    assert calls == []  # nothing to fetch -> verifier untouched


# --- verify_url_exists: HTTP-first, browser-fallback, blocked-tolerant --------


def _mock_http(status: int):
    """Patch httpx.Client so every request returns ``status`` (no network)."""
    real = httpx.Client

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, request=request)

    def factory(*_a: object, **_k: object) -> httpx.Client:
        return real(transport=httpx.MockTransport(handler))

    return factory


@pytest.mark.parametrize(
    "status,expected",
    [(200, "exists"), (204, "exists"), (404, "absent"), (410, "absent"),
     (403, "blocked"), (401, "blocked"), (429, "blocked"), (500, "blocked")],
)
def test_http_reach_status_mapping(monkeypatch, status, expected) -> None:
    monkeypatch.setattr(httpx, "Client", _mock_http(status))
    assert citations._http_reach("https://x.example/p", timeout=5) == expected


def test_http_reach_unreachable_on_transport_error(monkeypatch) -> None:
    def _boom(*_a: object, **_k: object) -> httpx.Client:
        raise httpx.ConnectError("refused")

    monkeypatch.setattr(httpx, "Client", _boom)
    assert citations._http_reach("https://x.example/p", timeout=5) == "unreachable"


def test_verify_url_blocked_counts_as_exists(monkeypatch) -> None:
    # A server that answers with an anti-bot 403 (e.g. TripAdvisor) is real → the
    # citation is accepted, and the (slow) browser is NOT consulted.
    monkeypatch.setattr(citations, "is_public_fetch_url", lambda _u: True)
    monkeypatch.setattr(citations, "_http_reach", lambda _u, *, timeout: "blocked")

    def _no_browser(_u: str) -> str:
        raise AssertionError("browser must not run when HTTP already answered")

    monkeypatch.setattr(citations, "_browser_reach", _no_browser)
    assert verify_url_exists("https://www.tripadvisor.com/Restaurants-g255060.html") is True


def test_verify_url_absent_is_false(monkeypatch) -> None:
    monkeypatch.setattr(citations, "is_public_fetch_url", lambda _u: True)
    monkeypatch.setattr(citations, "_http_reach", lambda _u, *, timeout: "absent")
    assert verify_url_exists("https://x.example/missing") is False


def test_verify_url_exists_passes(monkeypatch) -> None:
    monkeypatch.setattr(citations, "is_public_fetch_url", lambda _u: True)
    monkeypatch.setattr(citations, "_http_reach", lambda _u, *, timeout: "exists")
    assert verify_url_exists("https://x.example/ok") is True


def test_verify_url_unreachable_falls_back_to_browser(monkeypatch) -> None:
    # HTTP can't connect (TLS/fingerprint block) → a real browser may still reach it.
    monkeypatch.setattr(citations, "is_public_fetch_url", lambda _u: True)
    monkeypatch.setattr(citations, "_http_reach", lambda _u, *, timeout: "unreachable")
    monkeypatch.setattr(citations, "_browser_reach", lambda _u: "exists")
    assert verify_url_exists("https://x.example/js") is True
    monkeypatch.setattr(citations, "_browser_reach", lambda _u: "unreachable")
    assert verify_url_exists("https://x.example/js") is False


def test_verify_url_ssrf_short_circuits(monkeypatch) -> None:
    monkeypatch.setattr(citations, "is_public_fetch_url", lambda _u: False)

    def _boom(*_a: object, **_k: object) -> str:
        raise AssertionError("a non-public URL must never be probed")

    monkeypatch.setattr(citations, "_http_reach", _boom)
    assert verify_url_exists("http://127.0.0.1/admin") is False
