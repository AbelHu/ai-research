"""Tests for the pairing-codes repository (implementation-plan T7.5).

Offline + deterministic (injected clock). Verifies single-use, expiry, loose
typing (normalization), and that only a hash of the code is ever stored.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.storage.db import connect
from app.storage.migrations import migrate
from app.storage.repos import pairing_codes as repo


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


def test_mint_returns_plaintext_but_stores_only_hash(conn) -> None:
    minted = repo.mint_code(conn, now=_t())
    assert minted.code  # plaintext returned once
    row = conn.execute("SELECT code_hash FROM pairing_codes WHERE id = ?", (minted.id,)).fetchone()
    # The plaintext is never stored — only its sha256 hash.
    assert row["code_hash"] != minted.code
    assert len(row["code_hash"]) == 64  # sha256 hex
    assert repo.normalize_code(minted.code) not in row["code_hash"]


def test_consume_is_single_use(conn) -> None:
    minted = repo.mint_code(conn, now=_t())
    assert repo.consume_code(conn, minted.code, used_by="telegram:1", now=_t(10)) is True
    # Second use is rejected.
    assert repo.consume_code(conn, minted.code, used_by="telegram:1", now=_t(20)) is False
    row = conn.execute(
        "SELECT used_at, used_by FROM pairing_codes WHERE id = ?", (minted.id,)
    ).fetchone()
    assert row["used_at"] is not None
    assert row["used_by"] == "telegram:1"


def test_consume_accepts_loosely_typed_code(conn) -> None:
    minted = repo.mint_code(conn, now=_t())
    # Lower-case, spaces, and stripped dashes all normalize to the same code.
    typed = f"  {minted.code.replace('-', ' ').lower()}  "
    assert repo.consume_code(conn, typed, used_by="telegram:2", now=_t(5)) is True


def test_expired_code_is_rejected(conn) -> None:
    minted = repo.mint_code(conn, ttl_seconds=600, now=_t())
    # 11 minutes later — past the 10-minute TTL.
    assert repo.consume_code(conn, minted.code, used_by="telegram:3", now=_t(660)) is False


def test_unknown_code_is_rejected(conn) -> None:
    repo.mint_code(conn, now=_t())
    assert repo.consume_code(conn, "ZZZZ-ZZZZ", used_by="telegram:4", now=_t(5)) is False


def test_list_active_excludes_used_and_expired(conn) -> None:
    fresh = repo.mint_code(conn, ttl_seconds=600, now=_t())
    used = repo.mint_code(conn, ttl_seconds=600, now=_t())
    expired = repo.mint_code(conn, ttl_seconds=60, now=_t())

    repo.consume_code(conn, used.code, used_by="telegram:5", now=_t(10))

    active = repo.list_active(conn, now=_t(120))  # 2 min later: `expired` is gone
    active_ids = {c.id for c in active}
    assert fresh.id in active_ids
    assert used.id not in active_ids
    assert expired.id not in active_ids


def test_purge_expired_removes_only_stale_unused(conn) -> None:
    repo.mint_code(conn, ttl_seconds=60, now=_t())  # will expire
    fresh = repo.mint_code(conn, ttl_seconds=600, now=_t())
    removed = repo.purge_expired(conn, now=_t(120))
    assert removed == 1
    remaining = {c.id for c in repo.list_active(conn, now=_t(120))}
    assert remaining == {fresh.id}
