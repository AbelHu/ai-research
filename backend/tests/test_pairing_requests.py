"""Tests for the pairing-requests repository (implementation-plan T8.6).

Offline + deterministic (injected clock). Covers the user-initiated half of
request-and-approve: create/reuse a pending code, list, approve, and expiry.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.storage.db import connect
from app.storage.migrations import migrate
from app.storage.repos import pairing_requests as repo


@pytest.fixture
def conn():
    c = connect()
    migrate(c)
    try:
        yield c
    finally:
        c.close()


def _t(seconds: int = 0) -> datetime:
    return datetime(2026, 6, 15, 12, 0, 0, tzinfo=timezone.utc) + timedelta(seconds=seconds)


def test_create_request_returns_code_and_pending_row(conn) -> None:
    req = repo.create_or_refresh(conn, "telegram", "42", now=_t())
    assert req.code
    assert req.state == "pending"
    assert req.target == "telegram:42"
    # Listed as pending.
    pending = repo.list_pending(conn, now=_t(10))
    assert [p.target for p in pending] == ["telegram:42"]


def test_repeat_message_reuses_same_code(conn) -> None:
    first = repo.create_or_refresh(conn, "telegram", "42", now=_t())
    again = repo.create_or_refresh(conn, "telegram", "42", now=_t(30))
    assert again.code == first.code  # no spam: same pending code reused
    # Still a single row.
    assert len(repo.list_pending(conn, now=_t(40))) == 1


def test_expired_pending_request_gets_a_fresh_code(conn) -> None:
    first = repo.create_or_refresh(conn, "telegram", "42", ttl_seconds=900, now=_t())
    # 16 minutes later the old one is past TTL → a new code is issued.
    refreshed = repo.create_or_refresh(conn, "telegram", "42", now=_t(960))
    assert refreshed.code != first.code
    assert refreshed.state == "pending"


def test_approve_marks_approved(conn) -> None:
    req = repo.create_or_refresh(conn, "telegram", "42", now=_t())
    approved = repo.approve(conn, req.code, now=_t(60))
    assert approved is not None
    assert approved.state == "approved"
    assert approved.approved_at is not None
    assert approved.channel == "telegram"
    assert approved.channel_user_id == "42"


def test_approve_is_loose_about_formatting(conn) -> None:
    req = repo.create_or_refresh(conn, "telegram", "42", now=_t())
    typed = f"  {req.code.replace('-', ' ').lower()}  "  # spaces, lowercase, no dash
    assert repo.approve(conn, typed, now=_t(60)) is not None


def test_approve_rejects_unknown_used_and_expired(conn) -> None:
    assert repo.approve(conn, "ZZZZ-ZZZZ", now=_t()) is None  # unknown

    req = repo.create_or_refresh(conn, "telegram", "1", now=_t())
    repo.approve(conn, req.code, now=_t(10))
    assert repo.approve(conn, req.code, now=_t(20)) is None  # already approved

    other = repo.create_or_refresh(conn, "telegram", "2", ttl_seconds=900, now=_t())
    assert repo.approve(conn, other.code, now=_t(960)) is None  # expired


def test_approve_empty_code_is_none(conn) -> None:
    assert repo.approve(conn, "  ", now=_t()) is None


def test_list_pending_excludes_approved_and_expired(conn) -> None:
    a = repo.create_or_refresh(conn, "telegram", "1", ttl_seconds=900, now=_t())
    repo.create_or_refresh(conn, "telegram", "2", ttl_seconds=60, now=_t())  # will expire
    repo.create_or_refresh(conn, "telegram", "3", ttl_seconds=900, now=_t())
    repo.approve(conn, a.code, now=_t(10))

    pending = repo.list_pending(conn, now=_t(120))  # 2 min later
    assert {p.target for p in pending} == {"telegram:3"}


def test_expire_stale_flips_pending_to_expired(conn) -> None:
    repo.create_or_refresh(conn, "telegram", "1", ttl_seconds=60, now=_t())
    fresh = repo.create_or_refresh(conn, "telegram", "2", ttl_seconds=900, now=_t())
    changed = repo.expire_stale(conn, now=_t(120))
    assert changed == 1
    pending = {p.target for p in repo.list_pending(conn, now=_t(120))}
    assert pending == {fresh.target}
