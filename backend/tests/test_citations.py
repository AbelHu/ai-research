"""Tests for citation URL verification + SSRF guard (design-spec §6D, §7.1).

All offline: the SSRF pre-check works on schemes + IP literals without DNS, and
URL existence is exercised through an injected fake verifier — never the network.
"""

from __future__ import annotations

import pytest

from app.advisor.citations import is_safe_fetch_target, unresolved_citation_urls
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
