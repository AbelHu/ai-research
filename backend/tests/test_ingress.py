"""Tests for the gateway ingress (implementation-plan T8.3/T8.4).

Offline: the ask control loop runs against a per-role `FakeProvider` (as in
`test_ask_e2e`); pairing/allowlist hit a real in-memory DB. Covers the whole
inbound path — `/pair`, refusal, and an answered message — for a chat channel.
"""

from __future__ import annotations

import json

import pytest

from app.advisor.wrapper import Advisor
from app.channels.adapter import InboundMessage
from app.config.policies import Policies
from app.gateway.allowlist import REFUSED_ACTION, RefusalRateLimiter
from app.gateway.ingress import (
    PAIR_BAD_CODE,
    PAIR_HINT,
    PAIR_OK,
    handle_inbound,
    parse_pair_command,
)
from app.storage.db import connect
from app.storage.migrations import migrate
from app.storage.repos import audit as audit_repo
from app.storage.repos import identities as identities_repo
from app.storage.repos import pairing_codes as pairing_codes_repo
from tests.fakes import FakeProvider

ANALYSIS_ASK = json.dumps(
    {
        "belongs": True,
        "kind": "ask",
        "clarity": "clear",
        "complexity": "simple",
        "confidence": 0.95,
        "rationale": "a direct factual question",
    }
)
ANSWER = json.dumps(
    {
        "answer": "Paris is the capital of France.",
        "citations": [{"ref": "memory:1", "snippet": "capital of France is Paris"}],
        "confidence": 0.95,
    }
)


@pytest.fixture
def conn():
    c = connect()
    migrate(c)
    try:
        yield c
    finally:
        c.close()


def _advisor(conn) -> Advisor:
    providers = {"planner": FakeProvider(ANALYSIS_ASK), "drafter": FakeProvider(ANSWER)}
    return Advisor(resolve_provider=lambda role: providers[role], conn=conn)


def _inbound(text: str, *, user_id: str = "42", chat_id: str = "4242") -> InboundMessage:
    return InboundMessage(
        channel="telegram",
        channel_user_id=user_id,
        text=text,
        chat_id=chat_id,
        message_id="7",
    )


# --- parse_pair_command -----------------------------------------------------


def test_parse_pair_command() -> None:
    assert parse_pair_command("/pair ABCD-1234") == "ABCD-1234"
    assert parse_pair_command("/pair@mybot ABCD-1234") == "ABCD-1234"  # group style
    assert parse_pair_command("  /pair   code-here  ") == "code-here"
    assert parse_pair_command("/pair") == ""  # command, no code
    assert parse_pair_command("hello there") is None
    assert parse_pair_command("what is /pair") is None  # not a command


# --- /pair over chat (T8.4) -------------------------------------------------


def test_pair_with_valid_code_binds_and_confirms(conn) -> None:
    minted = pairing_codes_repo.mint_code(conn)
    result = handle_inbound(conn, _inbound(f"/pair {minted.code}"), advisor=_advisor(conn))

    assert result.action == "paired"
    assert result.reply is not None and result.reply.text == PAIR_OK
    # The sender is now a paired identity bound to the owner.
    identity = identities_repo.get_identity(conn, "telegram", "42")
    assert identity is not None and identity.is_paired
    assert result.user_id == identity.user_id


def test_pair_with_bad_code_is_refused(conn) -> None:
    result = handle_inbound(conn, _inbound("/pair NOPE-NOPE"), advisor=_advisor(conn))
    assert result.action == "bad_code"
    assert result.reply is not None and result.reply.text == PAIR_BAD_CODE
    assert identities_repo.get_identity(conn, "telegram", "42") is None


# --- allowlist refusal (T8.3) -----------------------------------------------


def test_unpaired_sender_is_refused_with_hint(conn) -> None:
    result = handle_inbound(
        conn, _inbound("what is 2+2?"), advisor=_advisor(conn), policy=Policies()
    )
    assert result.action == "refused"
    assert result.reply is not None and result.reply.text == PAIR_HINT
    # No request was created for an unpaired sender, and the refusal is audited.
    assert conn.execute("SELECT COUNT(*) FROM requests").fetchone()[0] == 0
    assert len(audit_repo.list_audit(conn, action=REFUSED_ACTION)) == 1


def test_silent_policy_refuses_without_reply(conn) -> None:
    result = handle_inbound(
        conn,
        _inbound("hello"),
        advisor=_advisor(conn),
        policy=Policies(unpaired_reply="silent"),
    )
    assert result.action == "refused"
    assert result.reply is None


def test_refusals_are_rate_limited(conn) -> None:
    limiter = RefusalRateLimiter(max_per_window=1, window_seconds=60, now=lambda: 0.0)
    policy = Policies()
    first = handle_inbound(
        conn,
        _inbound("hi", user_id="999"),
        advisor=_advisor(conn),
        policy=policy,
        rate_limiter=limiter,
    )
    second = handle_inbound(
        conn,
        _inbound("hi again", user_id="999"),
        advisor=_advisor(conn),
        policy=policy,
        rate_limiter=limiter,
    )
    assert first.reply is not None  # first refusal replies
    assert second.reply is None  # second within window is suppressed
    assert len(audit_repo.list_audit(conn, action=REFUSED_ACTION)) == 1


# --- answered path (T8.3) ---------------------------------------------------


def test_paired_sender_is_answered_end_to_end(conn) -> None:
    # Pair the sender first (host code), then ask.
    minted = pairing_codes_repo.mint_code(conn)
    handle_inbound(conn, _inbound(f"/pair {minted.code}"), advisor=_advisor(conn))

    result = handle_inbound(
        conn, _inbound("what is the capital of France?"), advisor=_advisor(conn)
    )

    assert result.action == "answered"
    assert result.reply is not None
    assert "Paris" in result.reply.text
    assert result.reply.chat_id == "4242"
    assert result.reply.reply_to_message_id == "7"
    # The ask actually ran (a request + job were created).
    assert conn.execute("SELECT COUNT(*) FROM requests").fetchone()[0] == 1


def test_paired_then_revoked_sender_is_refused(conn) -> None:
    minted = pairing_codes_repo.mint_code(conn)
    handle_inbound(conn, _inbound(f"/pair {minted.code}"), advisor=_advisor(conn))
    identities_repo.revoke_identity(conn, "telegram", "42")

    result = handle_inbound(conn, _inbound("anything"), advisor=_advisor(conn), policy=Policies())
    assert result.action == "refused"
