"""Paired-owner allowlist enforcement (design-spec §10.1; implementation-plan T7.4).

Every inbound message is checked against the ``user_identities`` allowlist
before any request or job is created:

* **Paired →** admitted; the resolved owner ``user_id`` rides along so the
  control loop can attribute the request (§6C).
* **Unpaired / revoked →** refused. **No request or job is created.** The
  refusal is written to ``audit_log`` and, depending on policy, a single
  "pair first" hint may be returned. Both the audit row and the hint are
  **rate-limited per sender** (a fixed window) so a flood of messages from an
  unknown account can't probe the bot or balloon the audit log.

The whole check is deterministic — the model is never asked who may chat.
"""

from __future__ import annotations

import sqlite3
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass

from app.config.policies import Policies, get_policies
from app.storage.repos import audit as audit_repo
from app.storage.repos import identities as identities_repo

# audit_log action for a refused inbound (stable string for querying/forensics).
REFUSED_ACTION = "gateway.refused_unpaired"


@dataclass(frozen=True)
class AllowDecision:
    """The Gateway's verdict for one inbound message."""

    admitted: bool
    reason: str  # "paired" | "unpaired" | "revoked"
    user_id: int | None = None  # the owner user id when admitted
    should_reply: bool = False  # send the "pair first" hint? (policy + rate limit)
    audited: bool = False  # whether this refusal was recorded (vs rate-suppressed)


class RefusalRateLimiter:
    """Fixed-window per-key limiter capping *actioned* refusals (§10.1).

    ``allow(key)`` returns ``True`` for the first ``max_per_window`` calls within
    any ``window_seconds`` window for that key, then ``False`` until the window
    rolls forward. The clock is injectable so tests are deterministic.
    """

    def __init__(
        self,
        *,
        max_per_window: int,
        window_seconds: float,
        now: Callable[[], float] = time.monotonic,
    ) -> None:
        self._max = max_per_window
        self._window = window_seconds
        self._now = now
        self._hits: dict[str, deque[float]] = {}

    def allow(self, key: str) -> bool:
        now = self._now()
        cutoff = now - self._window
        hits = self._hits.setdefault(key, deque())
        while hits and hits[0] < cutoff:
            hits.popleft()
        if len(hits) >= self._max:
            return False
        hits.append(now)
        return True


def _identity_key(channel: str, channel_user_id: str) -> str:
    return f"{channel}:{channel_user_id}"


def check_inbound(
    conn: sqlite3.Connection,
    channel: str,
    channel_user_id: str,
    *,
    policy: Policies | None = None,
    rate_limiter: RefusalRateLimiter | None = None,
) -> AllowDecision:
    """Decide whether a channel account may drive the system (§10.1).

    A ``paired`` binding is admitted with its owner ``user_id``. Anything else is
    refused: when the per-sender rate limit still allows it, the refusal is
    audited and (per ``unpaired_reply``) may carry a "pair first" hint; once the
    limiter trips, the refusal is silently dropped — no audit row, no reply.
    """
    policy = policy or get_policies()
    identity = identities_repo.get_identity(conn, channel, channel_user_id)

    if identity is not None and identity.is_paired:
        return AllowDecision(admitted=True, reason="paired", user_id=identity.user_id)

    reason = identity.state if identity is not None else "unpaired"

    # Rate-limit the refusal handling per sender to resist probing/flooding.
    if rate_limiter is not None and not rate_limiter.allow(_identity_key(channel, channel_user_id)):
        return AllowDecision(admitted=False, reason=reason, audited=False)

    audit_repo.record_audit(
        conn,
        actor="system",
        action=REFUSED_ACTION,
        target=_identity_key(channel, channel_user_id),
        payload={"reason": reason},
    )
    should_reply = policy.unpaired_reply == "pair_hint"
    return AllowDecision(admitted=False, reason=reason, should_reply=should_reply, audited=True)
