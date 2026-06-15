"""Pairing-requests repository (design-spec §10.1; implementation-plan T8.6).

The **user-initiated, host-approved** pairing path: an unpaired chat account
messages the bot, which records a **pending request** here and replies a short
``code``; the operator approves it on the trusted console
(``pair --approve <code>``), which binds the account.

The ``code`` is a **claim ticket**, not an authorization secret — possessing it
grants nothing without console access to approve — so it's stored in plaintext
(unlike host `pairing_codes`, where possession proves ownership and only a hash
is kept). One pending request per ``(channel, channel_user_id)``: a repeat
message **reuses the same code** (no spam).
"""

from __future__ import annotations

import secrets
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

# Unambiguous alphabet (no 0/O/1/I) for human-typeable codes.
_CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
_CODE_LEN = 8
DEFAULT_TTL_SECONDS = 900  # 15 minutes


@dataclass(frozen=True)
class PairingRequest:
    """A ``pairing_requests`` row (carries the plaintext claim-ticket code)."""

    id: int
    channel: str
    channel_user_id: str
    code: str
    state: str  # "pending" | "approved" | "expired"
    expires_at: str
    approved_at: str | None
    created_at: str

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> PairingRequest:
        return cls(
            id=row["id"],
            channel=row["channel"],
            channel_user_id=row["channel_user_id"],
            code=row["code"],
            state=row["state"],
            expires_at=row["expires_at"],
            approved_at=row["approved_at"],
            created_at=row["created_at"],
        )

    @property
    def target(self) -> str:
        return f"{self.channel}:{self.channel_user_id}"


def _utcnow() -> datetime:
    return datetime.now(tz=timezone.utc)


def _fmt(dt: datetime) -> str:
    """Format as the SQLite ``datetime('now')`` shape ('YYYY-MM-DD HH:MM:SS', UTC)."""
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def normalize_code(code: str) -> str:
    """Canonicalize a typed code (upper-case, no dashes/whitespace)."""
    return "".join(code.split()).replace("-", "").upper()


def _generate_code() -> str:
    """A random claim-ticket code, grouped as ``XXXX-XXXX`` for readability."""
    raw = "".join(secrets.choice(_CODE_ALPHABET) for _ in range(_CODE_LEN))
    return f"{raw[:4]}-{raw[4:]}"


def get_request(
    conn: sqlite3.Connection, channel: str, channel_user_id: str
) -> PairingRequest | None:
    """Return the request for a channel account, or ``None`` if none exists."""
    row = conn.execute(
        "SELECT * FROM pairing_requests WHERE channel = ? AND channel_user_id = ?",
        (channel, channel_user_id),
    ).fetchone()
    return PairingRequest.from_row(row) if row is not None else None


def create_or_refresh(
    conn: sqlite3.Connection,
    channel: str,
    channel_user_id: str,
    *,
    ttl_seconds: int = DEFAULT_TTL_SECONDS,
    now: datetime | None = None,
) -> PairingRequest:
    """Return the pending request for a sender, creating/refreshing as needed.

    A **pending, unexpired** request **reuses its existing code** (so a user who
    messages repeatedly isn't handed a new code each time). Otherwise (no row, or
    an expired/approved one) a fresh code + expiry is written.
    """
    moment = now or _utcnow()
    now_str = _fmt(moment)
    existing = get_request(conn, channel, channel_user_id)
    if existing is not None and existing.state == "pending" and existing.expires_at > now_str:
        return existing

    code = _generate_code()
    expires_at = _fmt(moment + timedelta(seconds=ttl_seconds))
    with conn:
        conn.execute(
            """
            INSERT INTO pairing_requests
                (channel, channel_user_id, code, state, expires_at, approved_at)
            VALUES (?, ?, ?, 'pending', ?, NULL)
            ON CONFLICT (channel, channel_user_id) DO UPDATE SET
                code        = excluded.code,
                state       = 'pending',
                expires_at  = excluded.expires_at,
                approved_at = NULL,
                created_at  = datetime('now')
            """,
            (channel, channel_user_id, code, expires_at),
        )
    refreshed = get_request(conn, channel, channel_user_id)
    assert refreshed is not None  # just upserted
    return refreshed


def list_pending(conn: sqlite3.Connection, *, now: datetime | None = None) -> list[PairingRequest]:
    """List pending, unexpired requests awaiting operator approval (oldest first)."""
    now_str = _fmt(now or _utcnow())
    rows = conn.execute(
        "SELECT * FROM pairing_requests WHERE state = 'pending' AND expires_at > ? ORDER BY id",
        (now_str,),
    ).fetchall()
    return [PairingRequest.from_row(r) for r in rows]


def approve(
    conn: sqlite3.Connection, code: str, *, now: datetime | None = None
) -> PairingRequest | None:
    """Mark a pending, unexpired request approved; return it (or ``None``).

    ``None`` means the code is unknown, already used, or expired. The match is
    loose (case/dash-insensitive) so the operator can type the code as shown. The
    actual allowlist binding is done by the pairing service (pure-DB here).
    """
    moment = now or _utcnow()
    now_str = _fmt(moment)
    row = _find_by_normalized(conn, code)
    if row is None:
        return None
    request = PairingRequest.from_row(row)
    if request.state != "pending" or request.expires_at <= now_str:
        return None
    with conn:
        conn.execute(
            "UPDATE pairing_requests SET state = 'approved', approved_at = ? WHERE id = ?",
            (now_str, request.id),
        )
    return PairingRequest.from_row(
        conn.execute("SELECT * FROM pairing_requests WHERE id = ?", (request.id,)).fetchone()
    )


def _find_by_normalized(conn: sqlite3.Connection, code: str) -> sqlite3.Row | None:
    """Find a request whose stored code matches the user-typed code loosely."""
    target = normalize_code(code)
    if not target:
        return None
    for row in conn.execute("SELECT * FROM pairing_requests").fetchall():
        if normalize_code(row["code"]) == target:
            return row
    return None


def expire_stale(conn: sqlite3.Connection, *, now: datetime | None = None) -> int:
    """Mark pending-but-expired requests ``expired``; return how many changed."""
    now_str = _fmt(now or _utcnow())
    with conn:
        cur = conn.execute(
            "UPDATE pairing_requests SET state = 'expired' "
            "WHERE state = 'pending' AND expires_at <= ?",
            (now_str,),
        )
    return cur.rowcount
