"""Pairing-codes repository (design-spec §10.1; implementation-plan T7.5).

Host-minted, **single-use, time-boxed** codes for the alternative pairing path:
the operator mints a code on the host (possessing it proves ownership, same
trust basis as the token cache, §7.2), then sends ``/pair <code>`` from the chat
account to bind it to the owner.

Only a **sha256 hash** of the code is stored — the plaintext is returned once at
mint time and never persisted (§12). Codes are normalized (upper-case,
dashes/whitespace stripped) before hashing so the user can type them loosely.
"""

from __future__ import annotations

import hashlib
import secrets
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

# Unambiguous alphabet (no 0/O/1/I) for human-typeable codes.
_CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
_CODE_LEN = 8
DEFAULT_TTL_SECONDS = 600  # 10 minutes (design-spec §10.1)


@dataclass(frozen=True)
class PairingCode:
    """A ``pairing_codes`` row (never carries the plaintext code)."""

    id: int
    code_hash: str
    expires_at: str
    used_at: str | None
    used_by: str | None
    created_at: str

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> PairingCode:
        return cls(
            id=row["id"],
            code_hash=row["code_hash"],
            expires_at=row["expires_at"],
            used_at=row["used_at"],
            used_by=row["used_by"],
            created_at=row["created_at"],
        )


@dataclass(frozen=True)
class MintedCode:
    """The result of minting: the one-time plaintext (show once) + its row id."""

    code: str
    id: int
    expires_at: str


def _utcnow() -> datetime:
    return datetime.now(tz=timezone.utc)


def _fmt(dt: datetime) -> str:
    """Format as the SQLite ``datetime('now')`` shape ('YYYY-MM-DD HH:MM:SS', UTC)."""
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def normalize_code(code: str) -> str:
    """Canonicalize a user-typed code (upper-case, no dashes/whitespace)."""
    return "".join(code.split()).replace("-", "").upper()


def _hash_code(code: str) -> str:
    return hashlib.sha256(normalize_code(code).encode("utf-8")).hexdigest()


def _generate_code() -> str:
    """A random single-use code, grouped as ``XXXX-XXXX`` for readability."""
    raw = "".join(secrets.choice(_CODE_ALPHABET) for _ in range(_CODE_LEN))
    return f"{raw[:4]}-{raw[4:]}"


def mint_code(
    conn: sqlite3.Connection,
    *,
    ttl_seconds: int = DEFAULT_TTL_SECONDS,
    now: datetime | None = None,
) -> MintedCode:
    """Mint a fresh single-use code; store only its hash, return the plaintext once."""
    now = now or _utcnow()
    code = _generate_code()
    expires_at = _fmt(now + timedelta(seconds=ttl_seconds))
    with conn:
        cur = conn.execute(
            "INSERT INTO pairing_codes (code_hash, expires_at) VALUES (?, ?)",
            (_hash_code(code), expires_at),
        )
    return MintedCode(code=code, id=int(cur.lastrowid), expires_at=expires_at)


def consume_code(
    conn: sqlite3.Connection,
    code: str,
    *,
    used_by: str,
    now: datetime | None = None,
) -> bool:
    """Atomically spend a code: returns ``True`` iff it was valid + unused + unexpired.

    On success the row is stamped ``used_at``/``used_by`` so it can't be reused.
    """
    now = now or _utcnow()
    now_str = _fmt(now)
    row = conn.execute(
        "SELECT id, expires_at, used_at FROM pairing_codes WHERE code_hash = ?",
        (_hash_code(code),),
    ).fetchone()
    if row is None or row["used_at"] is not None or row["expires_at"] <= now_str:
        return False
    with conn:
        cur = conn.execute(
            """
            UPDATE pairing_codes SET used_at = ?, used_by = ?
            WHERE id = ? AND used_at IS NULL
            """,
            (now_str, used_by, row["id"]),
        )
    return cur.rowcount > 0


def list_active(conn: sqlite3.Connection, *, now: datetime | None = None) -> list[PairingCode]:
    """List codes that are still mintable-fresh (unused and unexpired)."""
    now = now or _utcnow()
    rows = conn.execute(
        "SELECT * FROM pairing_codes WHERE used_at IS NULL AND expires_at > ? ORDER BY id",
        (_fmt(now),),
    ).fetchall()
    return [PairingCode.from_row(r) for r in rows]


def purge_expired(conn: sqlite3.Connection, *, now: datetime | None = None) -> int:
    """Delete expired, unused codes; returns the number removed (housekeeping)."""
    now = now or _utcnow()
    with conn:
        cur = conn.execute(
            "DELETE FROM pairing_codes WHERE used_at IS NULL AND expires_at <= ?",
            (_fmt(now),),
        )
    return cur.rowcount
