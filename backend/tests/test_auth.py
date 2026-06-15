"""Tests for the GitHub device-flow auth core (implementation-plan T7.1).

All offline: a single `httpx.MockTransport` routes the device-flow + token
endpoints by URL, so the whole login + exchange + cache lifecycle is exercised
without any network. The autouse network guard in conftest still applies.
"""

from __future__ import annotations

import json
import stat

import httpx
import pytest

from app.advisor.auth import (
    ACCESS_TOKEN_URL,
    COPILOT_TOKEN_URL,
    DEVICE_CODE_URL,
    GITHUB_USER_URL,
    AuthDenied,
    AuthExpired,
    GitHubCopilotAuth,
    NotLoggedIn,
    find_existing_oauth_token,
)


class FakeGitHub:
    """A scriptable GitHub device-flow + Copilot-token endpoint set."""

    def __init__(self) -> None:
        # Number of `authorization_pending` polls before the token is granted.
        self.pending_polls = 0
        self._polls = 0
        self.copilot_status = 200
        self.copilot_token = "copilot-abc"
        self.copilot_expires_at = 9_999_999_999
        self.user_login = "octocat"
        self.requests: list[httpx.Request] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        url = str(request.url)
        if url == DEVICE_CODE_URL:
            return httpx.Response(
                200,
                json={
                    "device_code": "dev-123",
                    "user_code": "WXYZ-1234",
                    "verification_uri": "https://github.com/login/device",
                    "interval": 1,
                    "expires_in": 900,
                },
            )
        if url == ACCESS_TOKEN_URL:
            self._polls += 1
            if self._polls <= self.pending_polls:
                return httpx.Response(200, json={"error": "authorization_pending"})
            return httpx.Response(
                200, json={"access_token": "gho_realtoken", "token_type": "bearer"}
            )
        if url == COPILOT_TOKEN_URL:
            if self.copilot_status != 200:
                return httpx.Response(self.copilot_status, json={"message": "no"})
            return httpx.Response(
                200,
                json={"token": self.copilot_token, "expires_at": self.copilot_expires_at},
            )
        if url == GITHUB_USER_URL:
            return httpx.Response(200, json={"login": self.user_login})
        return httpx.Response(404)  # pragma: no cover


@pytest.fixture
def fake_gh() -> FakeGitHub:
    return FakeGitHub()


def _auth(fake_gh: FakeGitHub, tmp_path, *, clock=None) -> GitHubCopilotAuth:
    def factory() -> httpx.Client:
        return httpx.Client(transport=httpx.MockTransport(fake_gh.handler))

    return GitHubCopilotAuth(
        cache_path=tmp_path / ".auth" / "github.json",
        client_factory=factory,
        now=clock or (lambda: 1000.0),
    )


def test_request_device_code(fake_gh, tmp_path) -> None:
    auth = _auth(fake_gh, tmp_path)
    device = auth.request_device_code()
    assert device.user_code == "WXYZ-1234"
    assert device.verification_uri == "https://github.com/login/device"
    assert device.interval == 1


def test_poll_returns_token_after_pending(fake_gh, tmp_path) -> None:
    fake_gh.pending_polls = 2
    auth = _auth(fake_gh, tmp_path)
    device = auth.request_device_code()
    token = auth.poll_for_oauth_token(device, sleep=lambda _s: None)
    assert token == "gho_realtoken"


def test_access_denied_raises(fake_gh, tmp_path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url) == ACCESS_TOKEN_URL:
            return httpx.Response(200, json={"error": "access_denied"})
        return fake_gh.handler(request)

    auth = GitHubCopilotAuth(
        cache_path=tmp_path / ".auth" / "github.json",
        client_factory=lambda: httpx.Client(transport=httpx.MockTransport(handler)),
        now=lambda: 1000.0,
    )
    device = auth.request_device_code()
    with pytest.raises(AuthDenied):
        auth.poll_for_oauth_token(device, sleep=lambda _s: None)


def test_poll_expires_when_deadline_passes(fake_gh, tmp_path) -> None:
    fake_gh.pending_polls = 10_000  # never approves
    clock = {"t": 1000.0}

    def now() -> float:
        return clock["t"]

    def sleep(_s: float) -> None:
        clock["t"] += 1000  # jump past the 900s expiry quickly

    auth = _auth(fake_gh, tmp_path, clock=now)
    device = auth.request_device_code()
    with pytest.raises(AuthExpired):
        auth.poll_for_oauth_token(device, sleep=sleep)


def test_exchange_for_copilot_token(fake_gh, tmp_path) -> None:
    auth = _auth(fake_gh, tmp_path)
    tok = auth.exchange_for_copilot_token("gho_realtoken")
    assert tok.token.reveal() == "copilot-abc"
    assert tok.expires_at == 9_999_999_999
    # The exchange sent the OAuth token as `Authorization: token ...`.
    exchange_req = [r for r in fake_gh.requests if str(r.url) == COPILOT_TOKEN_URL][-1]
    assert exchange_req.headers["Authorization"] == "token gho_realtoken"
    assert exchange_req.headers["Editor-Version"]


def test_save_and_get_bearer_exchanges_and_caches(fake_gh, tmp_path) -> None:
    auth = _auth(fake_gh, tmp_path)
    auth.save_oauth_token("gho_realtoken")

    bearer = auth.get_bearer()
    assert bearer == "copilot-abc"

    # Second call is served from cache (no new exchange request).
    exchanges_before = sum(1 for r in fake_gh.requests if str(r.url) == COPILOT_TOKEN_URL)
    auth.get_bearer()
    exchanges_after = sum(1 for r in fake_gh.requests if str(r.url) == COPILOT_TOKEN_URL)
    assert exchanges_after == exchanges_before  # reused the cached copilot token


def test_expired_copilot_token_is_refreshed(fake_gh, tmp_path) -> None:
    # Copilot token already (near) expired → get_bearer must re-exchange.
    fake_gh.copilot_expires_at = 1000  # == now, within the refresh margin
    auth = _auth(fake_gh, tmp_path, clock=lambda: 1000.0)
    auth.save_oauth_token("gho_realtoken")
    auth.get_bearer()  # first exchange
    first = sum(1 for r in fake_gh.requests if str(r.url) == COPILOT_TOKEN_URL)
    auth.get_bearer()  # expired → exchange again
    second = sum(1 for r in fake_gh.requests if str(r.url) == COPILOT_TOKEN_URL)
    assert second > first


def test_get_bearer_without_login_raises(fake_gh, tmp_path, monkeypatch) -> None:
    for env in ("COPILOT_GITHUB_TOKEN", "GH_TOKEN", "GITHUB_TOKEN"):
        monkeypatch.delenv(env, raising=False)
    auth = _auth(fake_gh, tmp_path)
    with pytest.raises(NotLoggedIn):
        auth.get_bearer()


def test_cache_file_is_permission_restricted(fake_gh, tmp_path) -> None:
    auth = _auth(fake_gh, tmp_path)
    auth.save_oauth_token("gho_realtoken")
    mode = stat.S_IMODE(auth.cache_path.stat().st_mode)
    assert mode == 0o600  # owner read/write only — never world/group readable
    # The token is in the cache file (it's the credential store) but...
    assert "gho_realtoken" in auth.cache_path.read_text(encoding="utf-8")


def test_cache_never_contains_redacted_or_repr_leak(fake_gh, tmp_path) -> None:
    auth = _auth(fake_gh, tmp_path)
    auth.save_oauth_token("gho_realtoken")
    auth.get_bearer()
    raw = json.loads(auth.cache_path.read_text(encoding="utf-8"))
    # The copilot token is stored as the plain value, not a Secret repr.
    assert raw["copilot_token"] == "copilot-abc"
    assert "Secret(" not in auth.cache_path.read_text(encoding="utf-8")


def test_is_logged_in_and_logout(fake_gh, tmp_path, monkeypatch) -> None:
    for env in ("COPILOT_GITHUB_TOKEN", "GH_TOKEN", "GITHUB_TOKEN"):
        monkeypatch.delenv(env, raising=False)
    auth = _auth(fake_gh, tmp_path)
    assert auth.is_logged_in() is False
    auth.save_oauth_token("gho_realtoken")
    assert auth.is_logged_in() is True
    auth.logout()
    assert auth.is_logged_in() is False


def test_find_existing_token_precedence_and_ghp_ignored() -> None:
    env = {"GH_TOKEN": "gho_fromenv", "GITHUB_TOKEN": "gho_other"}
    assert find_existing_oauth_token(env.get) == "gho_fromenv"
    # A classic ghp_ token is not usable for the Copilot exchange → ignored.
    assert find_existing_oauth_token({"GITHUB_TOKEN": "ghp_classic"}.get) is None
    assert find_existing_oauth_token({}.get) is None


def test_fetch_github_login(fake_gh, tmp_path) -> None:
    fake_gh.user_login = "abel"
    auth = _auth(fake_gh, tmp_path)
    assert auth.fetch_github_login("gho_realtoken") == "abel"
    # The /user call carried the token (used only to verify, then discarded).
    user_calls = [r for r in fake_gh.requests if str(r.url) == GITHUB_USER_URL]
    assert user_calls and user_calls[0].headers["Authorization"] == "token gho_realtoken"
