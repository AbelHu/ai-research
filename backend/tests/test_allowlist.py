"""Tests for the paired-owner allowlist gateway (implementation-plan T7.4).

Offline: a deterministic in-memory DB + an injected clock for the rate limiter.
Covers the identities/owner repo, the audit writer, and the gateway decision
(paired admitted; unpaired/revoked refused + audited; refusals rate-limited).
"""

from __future__ import annotations

import pytest

from app.config.policies import Policies
from app.config.settings import Settings
from app.gateway.allowlist import (
    REFUSED_ACTION,
    RefusalRateLimiter,
    check_inbound,
)
from app.storage.db import connect
from app.storage.migrations import migrate
from app.storage.repos import audit as audit_repo
from app.storage.repos import identities as identities_repo


@pytest.fixture
def conn():
    c = connect()
    migrate(c)
    try:
        yield c
    finally:
        c.close()


# --- identities / owner repo ------------------------------------------------


def test_ensure_owner_is_idempotent_and_single(conn) -> None:
    first = identities_repo.ensure_owner(conn)
    second = identities_repo.ensure_owner(conn)
    assert first == second
    count = conn.execute("SELECT COUNT(*) FROM users WHERE is_owner = 1").fetchone()[0]
    assert count == 1


def test_ensure_owner_records_github_login(conn) -> None:
    identities_repo.ensure_owner(conn)
    identities_repo.set_owner_github_login(conn, "octocat")
    owner = identities_repo.get_owner(conn)
    assert owner is not None
    assert owner.github_login == "octocat"
    assert owner.is_owner is True


def test_is_owner_login_matches_pin_case_insensitively(conn) -> None:
    settings = Settings(owner_github_login="OctoCat")
    assert identities_repo.is_owner_login(conn, "octocat", settings=settings) is True
    assert identities_repo.is_owner_login(conn, "someone-else", settings=settings) is False


def test_is_owner_login_strict_when_unestablished(conn) -> None:
    # No pin and no stored owner login → cannot verify → not the owner.
    settings = Settings(owner_github_login=None)
    assert identities_repo.is_owner_login(conn, "anyone", settings=settings) is False


def test_bind_and_revoke_roundtrip(conn) -> None:
    owner_id = identities_repo.ensure_owner(conn)
    binding = identities_repo.bind_identity(
        conn,
        user_id=owner_id,
        channel="telegram",
        channel_user_id="42",
        paired_via="host_code",
    )
    assert binding.is_paired
    assert binding.paired_via == "host_code"
    assert binding.paired_at is not None

    assert identities_repo.revoke_identity(conn, "telegram", "42") is True
    after = identities_repo.get_identity(conn, "telegram", "42")
    assert after is not None and after.state == "revoked"
    # Revoking again is a no-op (already revoked).
    assert identities_repo.revoke_identity(conn, "telegram", "42") is False


def test_bind_is_idempotent_repair_after_revoke(conn) -> None:
    owner_id = identities_repo.ensure_owner(conn)
    identities_repo.bind_identity(
        conn, user_id=owner_id, channel="telegram", channel_user_id="7", paired_via="device_flow"
    )
    identities_repo.revoke_identity(conn, "telegram", "7")
    # Re-pair flips the same row back to paired (no duplicate).
    again = identities_repo.bind_identity(
        conn, user_id=owner_id, channel="telegram", channel_user_id="7", paired_via="device_flow"
    )
    assert again.is_paired
    rows = identities_repo.list_identities(conn)
    assert len(rows) == 1


# --- gateway decision -------------------------------------------------------


def _pair(conn, channel="telegram", channel_user_id="100"):
    owner_id = identities_repo.ensure_owner(conn)
    identities_repo.bind_identity(
        conn,
        user_id=owner_id,
        channel=channel,
        channel_user_id=channel_user_id,
        paired_via="host_code",
    )
    return owner_id


def test_paired_sender_is_admitted(conn) -> None:
    owner_id = _pair(conn)
    decision = check_inbound(conn, "telegram", "100")
    assert decision.admitted is True
    assert decision.reason == "paired"
    assert decision.user_id == owner_id
    assert decision.should_reply is False
    # No refusal audit row for an admitted sender.
    assert audit_repo.list_audit(conn, action=REFUSED_ACTION) == []


def test_unpaired_sender_is_refused_and_audited(conn) -> None:
    decision = check_inbound(conn, "telegram", "999", policy=Policies())
    assert decision.admitted is False
    assert decision.reason == "unpaired"
    assert decision.user_id is None
    assert decision.should_reply is True  # default policy: pair_hint
    assert decision.audited is True

    refusals = audit_repo.list_audit(conn, action=REFUSED_ACTION)
    assert len(refusals) == 1
    assert refusals[0].target == "telegram:999"


def test_revoked_sender_is_refused_with_reason(conn) -> None:
    owner_id = identities_repo.ensure_owner(conn)
    identities_repo.bind_identity(
        conn, user_id=owner_id, channel="telegram", channel_user_id="5", paired_via="host_code"
    )
    identities_repo.revoke_identity(conn, "telegram", "5")

    decision = check_inbound(conn, "telegram", "5", policy=Policies())
    assert decision.admitted is False
    assert decision.reason == "revoked"
    assert decision.audited is True


def test_silent_policy_refuses_without_a_reply(conn) -> None:
    decision = check_inbound(conn, "telegram", "999", policy=Policies(unpaired_reply="silent"))
    assert decision.admitted is False
    assert decision.should_reply is False
    assert decision.audited is True  # still audited, just no hint


def test_refusals_are_rate_limited_per_sender(conn) -> None:
    clock = {"t": 0.0}
    limiter = RefusalRateLimiter(max_per_window=2, window_seconds=60, now=lambda: clock["t"])
    policy = Policies()

    # First two refusals in the window are actioned (audited + hint).
    d1 = check_inbound(conn, "telegram", "999", policy=policy, rate_limiter=limiter)
    d2 = check_inbound(conn, "telegram", "999", policy=policy, rate_limiter=limiter)
    assert d1.audited and d2.audited
    assert d1.should_reply and d2.should_reply

    # Third within the window is suppressed: no reply, no new audit row.
    d3 = check_inbound(conn, "telegram", "999", policy=policy, rate_limiter=limiter)
    assert d3.admitted is False
    assert d3.audited is False
    assert d3.should_reply is False
    assert len(audit_repo.list_audit(conn, action=REFUSED_ACTION)) == 2

    # A different sender has its own budget.
    d_other = check_inbound(conn, "telegram", "111", policy=policy, rate_limiter=limiter)
    assert d_other.audited is True

    # After the window rolls forward, the first sender is actioned again.
    clock["t"] = 61.0
    d4 = check_inbound(conn, "telegram", "999", policy=policy, rate_limiter=limiter)
    assert d4.audited is True
    assert len(audit_repo.list_audit(conn, action=REFUSED_ACTION)) == 4


def test_paired_sender_bypasses_the_rate_limiter(conn) -> None:
    _pair(conn, channel_user_id="100")
    limiter = RefusalRateLimiter(max_per_window=1, window_seconds=60, now=lambda: 0.0)
    # Many admits in a row never consume the refusal budget.
    for _ in range(5):
        assert check_inbound(conn, "telegram", "100", rate_limiter=limiter).admitted is True
