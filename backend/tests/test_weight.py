"""Tests for effective-weight + TTL math (implementation-plan T5.4)."""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from app.config.policies import MemoryPolicy
from app.memory.weight import (
    base_ttl_days,
    compute_expires_at,
    decay_lambda,
    effective_weight,
    reinforced_expiry,
)
from app.storage.db import connect
from app.storage.migrations import migrate
from app.storage.repos import memories as memories_repo

POLICY = MemoryPolicy()


@pytest.fixture
def conn():
    c = connect()
    migrate(c)
    try:
        yield c
    finally:
        c.close()


def _mem(conn, **over):
    return memories_repo.create_memory(conn, content="x", **over)


# --- TTL on write -----------------------------------------------------------


def test_base_ttl_scales_with_importance() -> None:
    low = base_ttl_days(POLICY, "short", 0.0)
    high = base_ttl_days(POLICY, "short", 1.0)
    assert high > low
    assert low == pytest.approx(POLICY.base_ttl_short_days)


def test_core_never_expires() -> None:
    assert base_ttl_days(POLICY, "core", 1.0) is None
    now = datetime(2026, 1, 1)
    assert compute_expires_at(now, "core", 1.0, POLICY) is None


def test_compute_expires_at_in_future() -> None:
    now = datetime(2026, 1, 1, 12, 0, 0)
    expires = compute_expires_at(now, "ephemeral", 0.0, POLICY)
    assert expires is not None
    assert datetime.fromisoformat(expires) > now


# --- decay rates ------------------------------------------------------------


def test_decay_lambda_per_class() -> None:
    assert decay_lambda(POLICY, "ephemeral") > decay_lambda(POLICY, "long")
    assert decay_lambda(POLICY, "core") == 0.0
    assert decay_lambda(POLICY, None) == 0.0


# --- effective weight -------------------------------------------------------


def test_weight_decays_with_age(conn) -> None:
    mem = _mem(conn, retention_class="short", importance=0.8, confidence=1.0)
    base = _parse_created(mem)
    fresh = effective_weight(mem, now=base, policy=POLICY)
    stale = effective_weight(mem, now=base + timedelta(days=30), policy=POLICY)
    assert stale < fresh


def test_core_does_not_decay(conn) -> None:
    mem = _mem(conn, retention_class="core", importance=0.9)
    base = _parse_created(mem)
    near = effective_weight(mem, now=base, policy=POLICY)
    far = effective_weight(mem, now=base + timedelta(days=365), policy=POLICY)
    assert far == pytest.approx(near)  # λ = 0 → no recency decay


def test_use_count_reinforces_weight(conn) -> None:
    cold = _mem(conn, retention_class="long", importance=0.5)
    base = _parse_created(cold)
    w0 = effective_weight(cold, now=base, policy=POLICY)
    # Bump use_count via the reinforcement layer, then re-read.
    from app.memory.reinforce import reinforce_memory

    reinforce_memory(conn, cold.id)
    reinforce_memory(conn, cold.id)
    warmed = memories_repo.get_memory(conn, cold.id)
    w_used = effective_weight(warmed, now=base, policy=POLICY)
    assert w_used > w0


def test_importance_and_confidence_scale_weight(conn) -> None:
    high = _mem(conn, retention_class="long", importance=0.9, confidence=1.0)
    low = _mem(conn, retention_class="long", importance=0.1, confidence=1.0)
    assert effective_weight(high, now=_parse_created(high), policy=POLICY) > effective_weight(
        low, now=_parse_created(low), policy=POLICY
    )


# --- reinforcement slide ----------------------------------------------------


def test_reinforced_expiry_slides_past_prior() -> None:
    now = datetime(2026, 1, 1)
    prior = (now + timedelta(days=5)).isoformat(sep=" ", timespec="seconds")
    slid = reinforced_expiry(prior, now, "long", 0.8, POLICY)
    assert slid is not None
    assert datetime.fromisoformat(slid) > datetime.fromisoformat(prior)


def test_reinforced_expiry_handles_lapsed(conn) -> None:
    now = datetime(2026, 6, 1)
    lapsed = "2000-01-01 00:00:00"  # already in the past
    slid = reinforced_expiry(lapsed, now, "short", 0.5, POLICY)
    assert datetime.fromisoformat(slid) > now  # anchored at now, not the past


def test_reinforced_expiry_core_stays_none() -> None:
    assert reinforced_expiry(None, datetime(2026, 1, 1), "core", 1.0, POLICY) is None


def _parse_created(mem) -> datetime:
    return datetime.fromisoformat(mem.created_at)
