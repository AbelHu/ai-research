"""Effective weight + TTL math (design-spec §9.1; implementation-plan T5.4).

Pure, deterministic functions — no DB, no model. They implement the spec's
retention clock and ranking weight:

    w_eff = importance · e^(−λ·Δt) · (1 + β·ln(1 + use_count)) · confidence

plus the base-TTL-on-write and the importance-scaled slide applied on a
reinforcing read. All knobs come from `MemoryPolicy` (`policies.yaml`).
"""

from __future__ import annotations

import math
from datetime import datetime, timedelta

from app.config.policies import MemoryPolicy
from app.storage.repos.memories import Memory

# Defaults for memories that never had these set (AI-suggested fields nullable).
_DEFAULT_IMPORTANCE = 0.5
_DEFAULT_CONFIDENCE = 1.0


def _parse_ts(ts: str) -> datetime:
    """Parse a SQLite ``datetime('now')`` string (``YYYY-MM-DD HH:MM:SS``)."""
    return datetime.fromisoformat(ts)


def decay_lambda(policy: MemoryPolicy, retention_class: str | None) -> float:
    """Per-day recency-decay rate λ for a class (``core`` and unknown → 0)."""
    return {
        "ephemeral": policy.decay_lambda_ephemeral,
        "short": policy.decay_lambda_short,
        "long": policy.decay_lambda_long,
    }.get(retention_class or "", 0.0)


def base_ttl_days(
    policy: MemoryPolicy, retention_class: str | None, importance: float
) -> float | None:
    """Base TTL (days) on write, scaled by ``(1 + importance)``.

    Returns ``None`` for ``core`` (and unknown classes) → no expiry.
    """
    base = {
        "ephemeral": policy.base_ttl_ephemeral_days,
        "short": policy.base_ttl_short_days,
        "long": policy.base_ttl_long_days,
    }.get(retention_class or "")
    if base is None:
        return None
    return base * (1.0 + importance)


def compute_expires_at(
    now: datetime,
    retention_class: str | None,
    importance: float,
    policy: MemoryPolicy,
) -> str | None:
    """TTL on write: ``now + base_ttl``. ``None`` for ``core`` (never expires)."""
    ttl = base_ttl_days(policy, retention_class, importance)
    if ttl is None:
        return None
    return (now + timedelta(days=ttl)).isoformat(sep=" ", timespec="seconds")


def reinforced_expiry(
    current_expires_at: str | None,
    now: datetime,
    retention_class: str | None,
    importance: float,
    policy: MemoryPolicy,
) -> str | None:
    """Slide expiry forward on a reinforcing read (§9.1).

    The new expiry is ``max(current, now) + importance·scale`` — always **past**
    whatever it was scheduled to be before the read. ``core`` never expires
    (stays ``None``); every other class (including an untyped item that already
    carries a TTL) slides.
    """
    if retention_class == "core":
        return None
    anchor = now
    if current_expires_at is not None:
        existing = _parse_ts(current_expires_at)
        anchor = max(existing, now)
    extension = policy.importance_ttl_scale_days * importance
    return (anchor + timedelta(days=extension)).isoformat(sep=" ", timespec="seconds")


def effective_weight(mem: Memory, *, now: datetime, policy: MemoryPolicy) -> float:
    """Compute the deterministic effective weight used for ranking + retention."""
    importance = mem.importance if mem.importance is not None else _DEFAULT_IMPORTANCE
    confidence = mem.confidence if mem.confidence is not None else _DEFAULT_CONFIDENCE
    lam = decay_lambda(policy, mem.retention_class)

    reference = mem.last_used_at or mem.created_at
    delta_days = max(0.0, (now - _parse_ts(reference)).total_seconds() / 86400.0)

    recency = math.exp(-lam * delta_days)
    reinforcement = 1.0 + policy.reinforce_beta * math.log1p(max(0, mem.use_count))
    return importance * recency * reinforcement * confidence
