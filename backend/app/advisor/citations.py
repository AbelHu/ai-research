"""Citation URL existence verification (design-spec §6D, §7.1).

Anti-hallucination guard for cited links: before an answer is accepted, any
**URL** carried in its citations must be **deterministically verified to exist**
(resolved + reachable). A fabricated or unreachable URL is treated as a
hallucination and the answer is repaired or escalated — never returned with a
made-up link. The experts reuse the same check when reviewing reports.

The fetch is plain deterministic code (never the AI) and is **SSRF-guarded**:
only ``http``/``https`` are allowed, and a host that resolves to a
private/loopback/link-local/reserved address is refused — so an AI-proposed URL
can't be used to reach internal services or a cloud metadata endpoint.

Everything here is injectable: callers pass a ``UrlVerifier`` so tests run fully
offline. ``http_url_exists`` is the real (network) default used in production.
"""

from __future__ import annotations

import ipaddress
import socket
from collections.abc import Callable, Iterable
from urllib.parse import urlparse

import httpx

from app.advisor.schemas import Source

# A URL verifier answers one question: does this URL exist / is it reachable?
UrlVerifier = Callable[[str], bool]

_ALLOWED_SCHEMES = {"http", "https"}


def is_safe_fetch_target(url: str) -> bool:
    """Static SSRF pre-check (no DNS): scheme + IP-literal host must be public.

    Returns ``False`` for a non-``http(s)`` scheme, a missing host, or a host
    given as an IP **literal** in a private/loopback/link-local/reserved range.
    A hostname (non-literal) passes here and is DNS-checked in
    :func:`http_url_exists` before any connection is made.
    """
    parts = urlparse(url)
    if parts.scheme not in _ALLOWED_SCHEMES or not parts.hostname:
        return False
    try:
        ip = ipaddress.ip_address(parts.hostname)
    except ValueError:
        return True  # a DNS name; resolved + re-checked at fetch time
    return ip.is_global


def _resolves_to_public_only(host: str) -> bool:
    """True iff every address ``host`` resolves to is globally routable."""
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror:
        return False
    addresses = {info[4][0] for info in infos}
    if not addresses:
        return False
    for addr in addresses:
        try:
            if not ipaddress.ip_address(addr).is_global:
                return False
        except ValueError:
            return False
    return True


def http_url_exists(url: str, *, timeout: float = 10.0) -> bool:
    """Return ``True`` if ``url`` exists and is reachable (SSRF-guarded).

    Tries a cheap ``HEAD`` first, falling back to ``GET`` for servers that don't
    support it; any ``< 400`` status counts as "exists". Any transport error,
    disallowed scheme, or non-public resolution returns ``False``.

    This performs network I/O and is therefore never exercised in the offline
    test suite — tests inject a fake ``UrlVerifier`` instead.
    """
    if not is_safe_fetch_target(url):
        return False
    host = urlparse(url).hostname
    if host is None or not _resolves_to_public_only(host):
        return False
    try:
        with httpx.Client(timeout=timeout, follow_redirects=True) as client:
            resp = client.head(url)
            if resp.status_code >= 400:
                resp = client.get(url)
            return resp.status_code < 400
    except httpx.HTTPError:
        return False


def unresolved_citation_urls(citations: Iterable[Source], verify: UrlVerifier) -> list[str]:
    """Return the provided citation URLs that ``verify`` reports as non-existent.

    Citations without a URL are skipped — there's nothing to fetch (e.g. a
    ``memory:`` reference). The returned list is empty when every provided URL
    checks out.
    """
    bad: list[str] = []
    for source in citations:
        url = source.url
        if url and not verify(url):
            bad.append(url)
    return bad
