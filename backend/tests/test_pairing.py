"""Tests for the owner-pairing service (implementation-plan T7.5).

Offline: the device-flow challenge runs against a mocked GitHub (device code +
token + ``/user``) via ``httpx.MockTransport``. Covers host-code binding,
device-flow owner binding, the non-owner refusal, and the host bootstrap that
establishes the owner login on the first challenge.
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
from app.config.settings import Settings
from app.gateway import pairing
from app.storage.db import connect
from app.storage.migrations import migrate
from app.storage.repos import audit as audit_repo
from app.storage.repos import identities as identities_repo
from app.storage.repos import pairing_codes as pairing_codes_repo


@pytest.fixture
def conn():
    c = connect()
    migrate(c)
    try:
        yield c
    finally:
        c.close()


def _auth_returning_login(login: str) -> GitHubCopilotAuth:
    """A GitHubCopilotAuth whose mocked device flow approves as ``login``."""

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


# --- host one-time code path ------------------------------------------------


def test_host_code_binds_owner(conn) -> None:
    minted = pairing_codes_repo.mint_code(conn)
    result = pairing.pair_with_host_code(
        conn, code=minted.code, channel="telegram", channel_user_id="42"
    )
    assert result.paired is True
    assert result.identity is not None
    assert result.identity.is_paired
    assert result.identity.paired_via == "host_code"
    # The bound identity points at the single owner user.
    owner = identities_repo.get_owner(conn)
    assert owner is not None and result.identity.user_id == owner.id
    # The success is audited.
    assert any(e.action == pairing.PAIRED_ACTION for e in audit_repo.list_audit(conn))


def test_bad_host_code_is_refused_and_audited(conn) -> None:
    result = pairing.pair_with_host_code(
        conn, code="NOPE-NOPE", channel="telegram", channel_user_id="42"
    )
    assert result.paired is False
    assert result.reason == "bad_code"
    assert identities_repo.get_identity(conn, "telegram", "42") is None
    assert any(e.action == pairing.BAD_CODE_ACTION for e in audit_repo.list_audit(conn))


# --- device-flow owner challenge -------------------------------------------


def test_device_flow_owner_is_paired(conn) -> None:
    settings = Settings(owner_github_login="octocat")
    auth = _auth_returning_login("octocat")

    result = pairing.run_device_flow_challenge(
        conn,
        auth,
        channel="telegram",
        channel_user_id="7",
        settings=settings,
        sleep=lambda _s: None,
    )

    assert result.paired is True
    assert result.github_login == "octocat"
    identity = identities_repo.get_identity(conn, "telegram", "7")
    assert identity is not None and identity.is_paired
    assert identity.paired_via == "device_flow"


def test_device_flow_non_owner_is_refused_and_audited(conn) -> None:
    settings = Settings(owner_github_login="octocat")
    auth = _auth_returning_login("imposter")

    result = pairing.run_device_flow_challenge(
        conn,
        auth,
        channel="telegram",
        channel_user_id="7",
        settings=settings,
        sleep=lambda _s: None,
    )

    assert result.paired is False
    assert result.reason == "not_owner"
    assert result.github_login == "imposter"
    # No binding, and the refusal is audited.
    assert identities_repo.get_identity(conn, "telegram", "7") is None
    assert any(e.action == pairing.REFUSED_NOT_OWNER_ACTION for e in audit_repo.list_audit(conn))


def test_host_bootstrap_establishes_owner_login(conn) -> None:
    # No owner login pinned or stored yet.
    settings = Settings(owner_github_login=None)
    auth = _auth_returning_login("first-owner")

    result = pairing.run_device_flow_challenge(
        conn,
        auth,
        channel="telegram",
        channel_user_id="1",
        settings=settings,
        bootstrap=True,
        sleep=lambda _s: None,
    )

    assert result.paired is True
    # The first host-run challenge bound the owner login to the approving account.
    owner = identities_repo.get_owner(conn)
    assert owner is not None and owner.github_login == "first-owner"


def test_chat_challenge_without_owner_is_refused(conn) -> None:
    # bootstrap=False (a chat-initiated challenge): with no owner established,
    # a stranger can't self-elect to owner.
    settings = Settings(owner_github_login=None)
    auth = _auth_returning_login("stranger")

    result = pairing.run_device_flow_challenge(
        conn,
        auth,
        channel="telegram",
        channel_user_id="1",
        settings=settings,
        bootstrap=False,
        sleep=lambda _s: None,
    )

    assert result.paired is False
    assert result.reason == "not_owner"
