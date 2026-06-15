"""Identity & owner-allowlist repository (design-spec §10.1; implementation-plan T7.4).

The bot is paired to a **single owner**. Two tables back that (§9):

* ``users`` — the owner is one row (``is_owner = 1``), identified by the GitHub
  account from the device-flow login (``github_login``).
* ``user_identities`` — the cross-channel **allowlist**: a ``(channel,
  channel_user_id)`` is admitted to chat **only** when its row is ``paired``.

This module is the deterministic read/write surface for both — the model is
never consulted on who may chat (§10.1). Pairing (writing a ``paired`` binding)
is driven by the gateway/CLI in T7.5; here we provide owner resolution, the
allowlist lookups, and the bind/revoke primitives.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from app.config.settings import Settings, get_settings

PairedVia = str  # "device_flow" | "host_code" (CHECKed in the schema)


@dataclass(frozen=True)
class User:
    """A ``users`` row (only the owner matters at single-user scale)."""

    id: int
    display_name: str | None
    github_login: str | None
    is_owner: bool
    created_at: str

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> User:
        return cls(
            id=row["id"],
            display_name=row["display_name"],
            github_login=row["github_login"],
            is_owner=bool(row["is_owner"]),
            created_at=row["created_at"],
        )


@dataclass(frozen=True)
class UserIdentity:
    """A ``user_identities`` row — one channel account's allowlist binding."""

    id: int
    user_id: int
    channel: str
    channel_user_id: str
    state: str  # "pending" | "paired" | "revoked"
    paired_via: str | None
    paired_at: str | None

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> UserIdentity:
        return cls(
            id=row["id"],
            user_id=row["user_id"],
            channel=row["channel"],
            channel_user_id=row["channel_user_id"],
            state=row["state"],
            paired_via=row["paired_via"],
            paired_at=row["paired_at"],
        )

    @property
    def is_paired(self) -> bool:
        return self.state == "paired"


# --- owner resolution -------------------------------------------------------


def get_owner(conn: sqlite3.Connection) -> User | None:
    """Return the single owner user, or ``None`` before it's been created."""
    row = conn.execute("SELECT * FROM users WHERE is_owner = 1 ORDER BY id LIMIT 1").fetchone()
    return User.from_row(row) if row is not None else None


def ensure_owner(
    conn: sqlite3.Connection,
    *,
    github_login: str | None = None,
    display_name: str = "owner",
) -> int:
    """Return the owner user's id, creating it on first use (§10.1).

    The canonical owner helper (the P4 control loop re-exports it). When a
    ``github_login`` is supplied it is recorded — binding the owner row to the
    GitHub account from the device-flow login — without clobbering an existing
    different value silently: it is only set when missing or explicitly updated.
    """
    owner = get_owner(conn)
    if owner is not None:
        if github_login and owner.github_login != github_login:
            with conn:
                conn.execute(
                    "UPDATE users SET github_login = ? WHERE id = ?",
                    (github_login, owner.id),
                )
        return owner.id
    with conn:
        cur = conn.execute(
            "INSERT INTO users (display_name, github_login, is_owner) VALUES (?, ?, 1)",
            (display_name, github_login),
        )
    return int(cur.lastrowid)


def set_owner_github_login(conn: sqlite3.Connection, github_login: str) -> int:
    """Bind the owner row to a GitHub login (creating the owner if needed)."""
    return ensure_owner(conn, github_login=github_login)


def expected_owner_login(
    conn: sqlite3.Connection, *, settings: Settings | None = None
) -> str | None:
    """The GitHub login a pairing login must match to be accepted as owner.

    Precedence: an explicit ``OWNER_GITHUB_LOGIN`` pin (settings) wins; otherwise
    the login already stored on the owner row. ``None`` means *not yet
    established* — the first host-driven pairing bootstraps it (T7.5).
    """
    settings = settings or get_settings()
    if settings.owner_github_login:
        return settings.owner_github_login
    owner = get_owner(conn)
    return owner.github_login if owner else None


def is_owner_login(
    conn: sqlite3.Connection, login: str, *, settings: Settings | None = None
) -> bool:
    """Whether ``login`` is the owner (case-insensitive GitHub-login match).

    Strict: when the owner login is not yet established this returns ``False``
    (we can't verify), so a chat device-flow challenge can't admit a stranger.
    The host-side ``pair`` flow is what bootstraps the owner login (T7.5).
    """
    expected = expected_owner_login(conn, settings=settings)
    if expected is None:
        return False
    return login.casefold() == expected.casefold()


# --- allowlist lookups ------------------------------------------------------


def get_identity(
    conn: sqlite3.Connection, channel: str, channel_user_id: str
) -> UserIdentity | None:
    """Return the binding for a channel account, or ``None`` if unknown."""
    row = conn.execute(
        "SELECT * FROM user_identities WHERE channel = ? AND channel_user_id = ?",
        (channel, channel_user_id),
    ).fetchone()
    return UserIdentity.from_row(row) if row is not None else None


def list_identities(conn: sqlite3.Connection, *, state: str | None = None) -> list[UserIdentity]:
    """List identity bindings, optionally filtered by ``state``."""
    if state is None:
        rows = conn.execute("SELECT * FROM user_identities ORDER BY id").fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM user_identities WHERE state = ? ORDER BY id", (state,)
        ).fetchall()
    return [UserIdentity.from_row(r) for r in rows]


def bind_identity(
    conn: sqlite3.Connection,
    *,
    user_id: int,
    channel: str,
    channel_user_id: str,
    paired_via: PairedVia,
) -> UserIdentity:
    """Upsert a channel account to ``paired`` for ``user_id`` (§10.1).

    Idempotent on ``(channel, channel_user_id)``: a re-pair (e.g. after a
    revoke) flips the existing row back to ``paired`` and restamps it.
    """
    with conn:
        conn.execute(
            """
            INSERT INTO user_identities
                (user_id, channel, channel_user_id, state, paired_via, paired_at)
            VALUES (?, ?, ?, 'paired', ?, datetime('now'))
            ON CONFLICT (channel, channel_user_id) DO UPDATE SET
                user_id    = excluded.user_id,
                state      = 'paired',
                paired_via = excluded.paired_via,
                paired_at  = datetime('now')
            """,
            (user_id, channel, channel_user_id, paired_via),
        )
    identity = get_identity(conn, channel, channel_user_id)
    assert identity is not None  # just upserted
    return identity


def revoke_identity(conn: sqlite3.Connection, channel: str, channel_user_id: str) -> bool:
    """Revoke a channel account's access. Returns ``True`` if a row changed."""
    with conn:
        cur = conn.execute(
            """
            UPDATE user_identities SET state = 'revoked'
            WHERE channel = ? AND channel_user_id = ? AND state != 'revoked'
            """,
            (channel, channel_user_id),
        )
    return cur.rowcount > 0
