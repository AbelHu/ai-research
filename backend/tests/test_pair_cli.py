"""Tests for the pair CLI (implementation-plan T7.5).

Offline: mint/list/revoke run against a temp DB; the device-flow challenge is
driven through a mocked GitHub. No network.
"""

from __future__ import annotations

import httpx
import pytest

from app.advisor.auth import (
    ACCESS_TOKEN_URL,
    DEVICE_CODE_URL,
    GITHUB_USER_URL,
    GitHubCopilotAuth,
)
from app.cli.pair import main
from app.config.settings import Settings
from app.storage.db import connect
from app.storage.migrations import migrate
from app.storage.repos import identities as identities_repo


@pytest.fixture
def db_path(tmp_path):
    return tmp_path / "app.db"


def _mock_auth(login: str) -> GitHubCopilotAuth:
    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url == DEVICE_CODE_URL:
            return httpx.Response(
                200,
                json={
                    "device_code": "dev-1",
                    "user_code": "CODE-1",
                    "verification_uri": "https://github.com/login/device",
                    "interval": 0,
                    "expires_in": 900,
                },
            )
        if url == ACCESS_TOKEN_URL:
            return httpx.Response(200, json={"access_token": "gho_x"})
        if url == GITHUB_USER_URL:
            return httpx.Response(200, json={"login": login})
        return httpx.Response(404)  # pragma: no cover

    return GitHubCopilotAuth(
        client_factory=lambda: httpx.Client(transport=httpx.MockTransport(handler)),
        now=lambda: 1000.0,
    )


@pytest.fixture(autouse=True)
def _no_owner_pin(monkeypatch):
    # Make owner resolution depend only on the DB (no env/.env pin) for determinism.
    monkeypatch.setattr(identities_repo, "get_settings", lambda: Settings(owner_github_login=None))


def test_mint_prints_a_code_and_persists_hash(db_path, capsys) -> None:
    rc = main(["--db", str(db_path)])  # default action = mint
    assert rc == 0
    out = capsys.readouterr().out
    assert "/pair " in out  # instructions include the code
    # A code row was persisted (hash only).
    conn = connect(db_path)
    try:
        count = conn.execute("SELECT COUNT(*) FROM pairing_codes").fetchone()[0]
    finally:
        conn.close()
    assert count == 1


def test_list_shows_active_codes(db_path, capsys) -> None:
    main(["--db", str(db_path)])  # mint one
    rc = main(["--db", str(db_path), "--list"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "Active pairing codes: 1" in out


def test_revoke_paired_account(db_path, capsys) -> None:
    conn = connect(db_path)
    migrate(conn)
    owner_id = identities_repo.ensure_owner(conn)
    identities_repo.bind_identity(
        conn, user_id=owner_id, channel="telegram", channel_user_id="42", paired_via="host_code"
    )
    conn.close()

    rc = main(["--db", str(db_path), "--revoke", "telegram:42"])
    assert rc == 0

    conn = connect(db_path)
    try:
        identity = identities_repo.get_identity(conn, "telegram", "42")
    finally:
        conn.close()
    assert identity is not None and identity.state == "revoked"


def test_revoke_unknown_account_returns_nonzero(db_path) -> None:
    assert main(["--db", str(db_path), "--revoke", "telegram:does-not-exist"]) == 1


def test_revoke_bad_target_format(db_path) -> None:
    assert main(["--db", str(db_path), "--revoke", "no-colon"]) == 1


def test_challenge_binds_owner(db_path, monkeypatch, capsys) -> None:
    monkeypatch.setattr("app.cli.pair.GitHubCopilotAuth", lambda: _mock_auth("owner1"))
    rc = main(
        [
            "--db",
            str(db_path),
            "--challenge",
            "--channel",
            "telegram",
            "--user",
            "9",
            "--no-browser",
        ]
    )
    assert rc == 0
    conn = connect(db_path)
    try:
        identity = identities_repo.get_identity(conn, "telegram", "9")
    finally:
        conn.close()
    assert identity is not None and identity.is_paired


def test_challenge_refuses_non_owner(db_path, monkeypatch) -> None:
    # Pre-establish a different owner so the approving account isn't the owner.
    conn = connect(db_path)
    migrate(conn)
    identities_repo.set_owner_github_login(conn, "realowner")
    conn.close()

    monkeypatch.setattr("app.cli.pair.GitHubCopilotAuth", lambda: _mock_auth("stranger"))
    rc = main(
        [
            "--db",
            str(db_path),
            "--challenge",
            "--channel",
            "telegram",
            "--user",
            "9",
            "--no-browser",
        ]
    )
    assert rc == 1
    conn = connect(db_path)
    try:
        assert identities_repo.get_identity(conn, "telegram", "9") is None
    finally:
        conn.close()


def test_challenge_requires_channel_and_user(db_path) -> None:
    assert main(["--db", str(db_path), "--challenge"]) == 1
