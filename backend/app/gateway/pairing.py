"""Owner pairing service (design-spec §10.1; implementation-plan T7.5).

Binds a chat account ``(channel, channel_user_id)`` to the **owner** via one of
two deterministic paths, then writes the ``user_identities`` allowlist row:

* **Host one-time code** — the operator minted a code on the host (proving
  ownership); spending a valid, unused, unexpired code binds the sender.
* **Device-flow owner challenge** — the chat user approves the GitHub device
  flow; we read their ``login`` and bind **only** if it is the owner. The raw
  token is used solely for that check and **discarded** (never cached).

Every outcome is audited. The model is never consulted — who may chat is
decided entirely by deterministic code (§10.1). This service is channel-agnostic
so the host CLI (T7.5) and the chat ``/pair`` handler (P8) share it.
"""

from __future__ import annotations

import sqlite3
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime

from app.advisor.auth import DeviceCode, GitHubCopilotAuth
from app.config.settings import Settings
from app.storage.repos import audit as audit_repo
from app.storage.repos import identities as identities_repo
from app.storage.repos import pairing_codes as pairing_codes_repo
from app.storage.repos.identities import UserIdentity

# audit_log actions (stable strings for forensics/querying).
PAIRED_ACTION = "pairing.paired"
REFUSED_NOT_OWNER_ACTION = "pairing.refused_not_owner"
BAD_CODE_ACTION = "pairing.bad_code"


@dataclass(frozen=True)
class PairResult:
    """The outcome of a pairing attempt."""

    paired: bool
    reason: str  # "paired" | "bad_code" | "not_owner"
    identity: UserIdentity | None = None
    github_login: str | None = None


def _target(channel: str, channel_user_id: str) -> str:
    return f"{channel}:{channel_user_id}"


def pair_with_host_code(
    conn: sqlite3.Connection,
    *,
    code: str,
    channel: str,
    channel_user_id: str,
    now: datetime | None = None,
) -> PairResult:
    """Bind a chat account by spending a host-minted one-time code (§10.1)."""
    target = _target(channel, channel_user_id)
    if not pairing_codes_repo.consume_code(conn, code, used_by=target, now=now):
        audit_repo.record_audit(conn, actor="user", action=BAD_CODE_ACTION, target=target)
        return PairResult(paired=False, reason="bad_code")

    owner_id = identities_repo.ensure_owner(conn)
    identity = identities_repo.bind_identity(
        conn,
        user_id=owner_id,
        channel=channel,
        channel_user_id=channel_user_id,
        paired_via="host_code",
    )
    audit_repo.record_audit(
        conn, actor="system", action=PAIRED_ACTION, target=target, payload={"via": "host_code"}
    )
    return PairResult(paired=True, reason="paired", identity=identity)


def bind_verified_owner(
    conn: sqlite3.Connection,
    *,
    login: str,
    channel: str,
    channel_user_id: str,
    settings: Settings | None = None,
    bootstrap: bool = False,
) -> PairResult:
    """Bind a chat account given a **verified** GitHub login, iff it is the owner.

    ``bootstrap`` (host-driven first pairing only) lets the very first challenge
    establish the owner login when none is pinned/stored yet — the operator at
    the shell is trusted. From a chat-initiated challenge (P8) ``bootstrap`` is
    ``False``, so a stranger can never self-elect to owner.
    """
    target = _target(channel, channel_user_id)
    if bootstrap and identities_repo.expected_owner_login(conn, settings=settings) is None:
        identities_repo.set_owner_github_login(conn, login)

    if not identities_repo.is_owner_login(conn, login, settings=settings):
        audit_repo.record_audit(
            conn,
            actor="system",
            action=REFUSED_NOT_OWNER_ACTION,
            target=target,
            payload={"login": login},
        )
        return PairResult(paired=False, reason="not_owner", github_login=login)

    owner_id = identities_repo.ensure_owner(conn, github_login=login)
    identity = identities_repo.bind_identity(
        conn,
        user_id=owner_id,
        channel=channel,
        channel_user_id=channel_user_id,
        paired_via="device_flow",
    )
    audit_repo.record_audit(
        conn,
        actor="system",
        action=PAIRED_ACTION,
        target=target,
        payload={"via": "device_flow", "login": login},
    )
    return PairResult(paired=True, reason="paired", identity=identity, github_login=login)


def run_device_flow_challenge(
    conn: sqlite3.Connection,
    auth: GitHubCopilotAuth,
    *,
    channel: str,
    channel_user_id: str,
    settings: Settings | None = None,
    bootstrap: bool = False,
    on_prompt: Callable[[DeviceCode], None] | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> PairResult:
    """Full device-flow owner challenge → verify ``login`` → bind (token discarded).

    ``on_prompt`` is called with the device code so the caller can show the user
    code + verification URL (the CLI prints it; the chat ``/pair`` surfaces it).
    """
    device = auth.request_device_code()
    if on_prompt is not None:
        on_prompt(device)
    oauth_token = auth.poll_for_oauth_token(device, sleep=sleep)
    # Verify ownership, then discard the token — never cached (§10.1).
    login = auth.fetch_github_login(oauth_token)
    return bind_verified_owner(
        conn,
        login=login,
        channel=channel,
        channel_user_id=channel_user_id,
        settings=settings,
        bootstrap=bootstrap,
    )
