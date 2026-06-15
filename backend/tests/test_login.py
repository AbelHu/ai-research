"""Tests for the login CLI (implementation-plan T7.3).

The device flow is driven against the same mocked GitHub endpoints as
`test_auth.py`, so the CLI is exercised end-to-end without network.
"""

from __future__ import annotations

import httpx
import pytest

from app.advisor.auth import (
    ACCESS_TOKEN_URL,
    COPILOT_TOKEN_URL,
    DEVICE_CODE_URL,
    GitHubCopilotAuth,
)
from app.cli.login import main, run_login


def _fake_handler(*, granted: bool = True):
    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url == DEVICE_CODE_URL:
            return httpx.Response(
                200,
                json={
                    "device_code": "dev-1",
                    "user_code": "CODE-1",
                    "verification_uri": "https://github.com/login/device",
                    "interval": 1,
                    "expires_in": 900,
                },
            )
        if url == ACCESS_TOKEN_URL:
            if granted:
                return httpx.Response(200, json={"access_token": "gho_x"})
            return httpx.Response(200, json={"error": "access_denied"})
        if url == COPILOT_TOKEN_URL:
            return httpx.Response(200, json={"token": "cop-1", "expires_at": 9_999_999_999})
        return httpx.Response(404)  # pragma: no cover

    return handler


@pytest.fixture(autouse=True)
def _no_env_tokens(monkeypatch):
    for env in ("COPILOT_GITHUB_TOKEN", "GH_TOKEN", "GITHUB_TOKEN"):
        monkeypatch.delenv(env, raising=False)


def _auth(tmp_path, handler) -> GitHubCopilotAuth:
    return GitHubCopilotAuth(
        cache_path=tmp_path / ".auth" / "github.json",
        client_factory=lambda: httpx.Client(transport=httpx.MockTransport(handler)),
        now=lambda: 1000.0,
    )


def test_login_success_caches_token(tmp_path, capsys) -> None:
    auth = _auth(tmp_path, _fake_handler(granted=True))
    rc = run_login(auth, open_browser=False)
    assert rc == 0
    assert auth.is_logged_in()
    out = capsys.readouterr().out
    assert "CODE-1" in out  # the user code was shown
    assert "Logged in" in out


def test_login_denied_returns_error(tmp_path, capsys) -> None:
    auth = _auth(tmp_path, _fake_handler(granted=False))
    rc = run_login(auth, open_browser=False)
    assert rc == 1
    assert not auth.is_logged_in()


def test_status_flag_when_logged_out(tmp_path, monkeypatch) -> None:
    auth = _auth(tmp_path, _fake_handler())
    monkeypatch.setattr("app.cli.login.GitHubCopilotAuth", lambda: auth)
    assert main(["--status"]) == 1  # not logged in → non-zero


def test_status_flag_when_logged_in(tmp_path, monkeypatch) -> None:
    auth = _auth(tmp_path, _fake_handler())
    auth.save_oauth_token("gho_x")
    monkeypatch.setattr("app.cli.login.GitHubCopilotAuth", lambda: auth)
    assert main(["--status"]) == 0


def test_logout_flag_removes_cache(tmp_path, monkeypatch) -> None:
    auth = _auth(tmp_path, _fake_handler())
    auth.save_oauth_token("gho_x")
    assert auth.is_logged_in()
    monkeypatch.setattr("app.cli.login.GitHubCopilotAuth", lambda: auth)
    assert main(["--logout"]) == 0
    assert not auth.is_logged_in()


def test_login_reuses_env_token(tmp_path, monkeypatch, capsys) -> None:
    monkeypatch.setenv("GH_TOKEN", "gho_fromenv")
    auth = _auth(tmp_path, _fake_handler())
    rc = run_login(auth, open_browser=False)
    assert rc == 0
    assert "Reusing an existing GitHub token" in capsys.readouterr().out
