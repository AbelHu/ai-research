"""Pair a chat account to the owner (design-spec §10.1; implementation-plan T7.5/T8.7).

Run from the ``backend/`` directory:

    python -m app.cli.pair                       # mint a one-time host code
    python -m app.cli.pair --list                # pending requests + codes + paired accounts
    python -m app.cli.pair --approve ABCD-1234   # approve a user's pending request
    python -m app.cli.pair --revoke telegram:42  # revoke a paired account
    python -m app.cli.pair --challenge --channel telegram --user 42
                                                 # device-flow owner challenge → bind

Ways to bind a chat account to the **owner** (§10.1):

* **Request-and-approve (recommended).** The user messages the bot; the bot
  replies a **pairing code**. The operator runs ``--list`` to see pending
  requests and ``--approve <code>`` to bind that account — approval happens on
  the trusted console, so the model/user never self-authorizes.
* **Host code.** Mint a short, single-use code here (``--mint``); send
  ``/pair <code>`` from the chat account to bind it. Only a hash is stored.
* **Device-flow challenge.** Approve the GitHub device flow **as the owner**;
  the bot reads your ``login`` and binds the given ``(channel, user)``.

Pairing and the allowlist are **deterministic** — the model is never consulted.
"""

from __future__ import annotations

import argparse
import sys
import webbrowser
from pathlib import Path

from dotenv import load_dotenv

from app.advisor.auth import AuthError, DeviceCode, GitHubCopilotAuth
from app.config.settings import REPO_ROOT
from app.gateway import pairing
from app.storage.db import connect
from app.storage.migrations import migrate
from app.storage.repos import identities as identities_repo
from app.storage.repos import pairing_codes as pairing_codes_repo
from app.storage.repos import pairing_requests as pairing_requests_repo

DEFAULT_DB_NAME = "app.db"


def run_mint(conn, *, ttl_seconds: int = pairing_codes_repo.DEFAULT_TTL_SECONDS) -> int:
    """Mint a one-time host pairing code and print it (shown once)."""
    minted = pairing_codes_repo.mint_code(conn, ttl_seconds=ttl_seconds)
    print(f"[ok]   Pairing code (single-use, expires {minted.expires_at} UTC):\n")
    print(f"         {minted.code}\n")
    print("       From the chat account, send:  /pair " + minted.code)
    return 0


def run_list(conn) -> int:
    """List pending pairing requests + active host codes + paired accounts."""
    pending = pairing_requests_repo.list_pending(conn)
    print(f"[info] Pending pairing requests: {len(pending)}")
    for req in pending:
        print(
            f"         {req.code}  {req.channel}:{req.channel_user_id}"
            f"  requested {req.created_at} UTC"
        )
    if pending:
        print("       Approve one with:  python -m app.cli.pair --approve <code>")

    active = pairing_codes_repo.list_active(conn)
    print(f"[info] Active host codes: {len(active)}")
    for code in active:
        print(f"         #{code.id}  expires {code.expires_at} UTC  (available)")

    paired = identities_repo.list_identities(conn, state="paired")
    print(f"[info] Paired accounts: {len(paired)}")
    for identity in paired:
        print(
            f"         {identity.channel}:{identity.channel_user_id}"
            f"  via {identity.paired_via}  at {identity.paired_at}"
        )
    return 0


def run_approve(conn, code: str) -> int:
    """Approve a user's pending pairing request (binds the requesting account)."""
    result = pairing.approve_pairing_request(conn, code)
    if result.paired:
        ident = result.identity
        print(f"[done] Approved — {ident.channel}:{ident.channel_user_id} can now chat.")
        return 0
    print(f"[fail] No pending request for code {code!r} (unknown, already used, or expired).")
    return 1


def run_revoke(conn, target: str) -> int:
    """Revoke a paired account given a ``channel:channel_user_id`` target."""
    if ":" not in target:
        print(f"[fail] Expected <channel:user>, got: {target!r}")
        return 1
    channel, channel_user_id = target.split(":", 1)
    if identities_repo.revoke_identity(conn, channel, channel_user_id):
        print(f"[ok]   Revoked {target} — that account can no longer chat.")
        return 0
    print(f"[info] Nothing to revoke for {target} (not paired).")
    return 1


def _print_challenge(device: DeviceCode) -> None:
    print()
    print("To pair this account, approve as the OWNER GitHub account:")
    print(f"  1. Open: {device.verification_uri}")
    print(f"  2. Enter the code: {device.user_code}")
    print()
    print("Waiting for approval… (Ctrl-C to cancel)")


def run_challenge(
    conn,
    auth: GitHubCopilotAuth,
    *,
    channel: str,
    channel_user_id: str,
    open_browser: bool = True,
) -> int:
    """Run the device-flow owner challenge and bind the given chat account."""

    def on_prompt(device: DeviceCode) -> None:
        _print_challenge(device)
        if open_browser:
            try:
                webbrowser.open(device.verification_uri)
            except Exception:  # noqa: BLE001 - opening a browser is best-effort
                pass

    try:
        # Host-run challenge: bootstrap the owner login if none is set yet.
        result = pairing.run_device_flow_challenge(
            conn,
            auth,
            channel=channel,
            channel_user_id=channel_user_id,
            bootstrap=True,
            on_prompt=on_prompt,
        )
    except AuthError as exc:
        print(f"[fail] Device-flow challenge did not complete: {exc}")
        return 1

    if result.paired:
        print(f"[done] Paired {channel}:{channel_user_id} → owner ({result.github_login}).")
        return 0
    print(f"[fail] Refused: the approved account ({result.github_login}) is not the owner.")
    return 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m app.cli.pair",
        description="Pair a chat account to the owner (host code or device-flow challenge).",
    )
    parser.add_argument(
        "--db", type=Path, default=None, help="database file (default: data/app.db)"
    )
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--mint", action="store_true", help="mint a one-time host code (default)")
    group.add_argument(
        "--list", action="store_true", help="list pending requests + codes + paired accounts"
    )
    group.add_argument(
        "--approve", metavar="CODE", help="approve a user's pending pairing request by code"
    )
    group.add_argument("--revoke", metavar="CHANNEL:USER", help="revoke a paired account")
    group.add_argument(
        "--challenge", action="store_true", help="run the device-flow owner challenge"
    )
    parser.add_argument("--channel", help="channel for --challenge (e.g. telegram)")
    parser.add_argument("--user", help="channel user id for --challenge")
    parser.add_argument(
        "--no-browser", action="store_true", help="do not try to open the browser (challenge)"
    )
    args = parser.parse_args(argv)

    load_dotenv(REPO_ROOT / ".env", override=False)
    db_path = args.db or (REPO_ROOT / "data" / DEFAULT_DB_NAME)
    conn = connect(db_path)
    try:
        migrate(conn)
        if args.list:
            return run_list(conn)
        if args.approve:
            return run_approve(conn, args.approve)
        if args.revoke:
            return run_revoke(conn, args.revoke)
        if args.challenge:
            if not args.channel or not args.user:
                print("[fail] --challenge requires --channel and --user.")
                return 1
            return run_challenge(
                conn,
                GitHubCopilotAuth(),
                channel=args.channel,
                channel_user_id=args.user,
                open_browser=not args.no_browser,
            )
        # Default action: mint a code.
        return run_mint(conn)
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
