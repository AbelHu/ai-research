"""Log in to GitHub Copilot via device flow (design-spec §7.2; implementation-plan T7.3).

Run from the ``backend/`` directory:

    python -m app.cli.login            # start the device-flow login
    python -m app.cli.login --status   # show whether a token is already cached
    python -m app.cli.login --logout   # remove the cached credentials

The flow prints a **user code** + verification URL; you approve it in your
browser, and the CLI polls until GitHub grants a token, then caches it (the
git-ignored ``data/.auth/github.json``, perms 600). After logging in, point a
model-role at a ``github_copilot`` provider in ``config/models.yaml`` and the
core uses your GitHub account — no PAT needed.

This is the live, opt-in login path (network); the device flow itself is unit-
tested offline against mocked endpoints (`tests/test_auth.py`).
"""

from __future__ import annotations

import argparse
import sys
import webbrowser

from app.advisor.auth import (
    AuthError,
    DeviceCode,
    GitHubCopilotAuth,
    find_existing_oauth_token,
)


def _print_challenge(device: DeviceCode) -> None:
    print()
    print("To finish signing in:")
    print(f"  1. Open: {device.verification_uri}")
    print(f"  2. Enter the code: {device.user_code}")
    print()
    print("Waiting for you to approve in the browser… (Ctrl-C to cancel)")


def run_login(auth: GitHubCopilotAuth, *, open_browser: bool = True) -> int:
    """Drive the device flow, cache the token, and verify the exchange works."""
    # Convenience: reuse an existing GH token from the environment if present.
    existing = find_existing_oauth_token()
    if existing is not None:
        print("[ok]   Reusing an existing GitHub token from the environment.")
        auth.save_oauth_token(existing)
    else:
        try:
            device = auth.request_device_code()
        except (AuthError, OSError) as exc:
            print(f"[fail] Could not start the device flow: {exc}")
            return 1

        _print_challenge(device)
        if open_browser:
            try:
                webbrowser.open(device.verification_uri)
            except Exception:  # noqa: BLE001 - opening a browser is best-effort
                pass

        try:
            oauth_token = auth.poll_for_oauth_token(device)
        except AuthError as exc:
            print(f"[fail] Login did not complete: {exc}")
            return 1
        auth.save_oauth_token(oauth_token)

    # Verify the OAuth token can be exchanged for a Copilot API token.
    try:
        auth.get_bearer()
    except AuthError as exc:
        print(f"[fail] Logged in, but the Copilot token exchange failed: {exc}")
        print("       Your GitHub account may not have Copilot access.")
        return 1

    print("[done] Logged in. Credentials cached at:")
    print(f"       {auth.cache_path}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m app.cli.login",
        description="Log in to GitHub Copilot via device flow (Route A).",
    )
    parser.add_argument("--status", action="store_true", help="show whether a token is cached")
    parser.add_argument("--logout", action="store_true", help="remove the cached credentials")
    parser.add_argument(
        "--no-browser", action="store_true", help="do not try to open the browser automatically"
    )
    args = parser.parse_args(argv)

    auth = GitHubCopilotAuth()

    if args.logout:
        auth.logout()
        print("[ok]   Logged out (cached credentials removed).")
        return 0

    if args.status:
        if auth.is_logged_in():
            print(f"[ok]   Logged in. Credentials at: {auth.cache_path}")
            return 0
        print("[info] Not logged in. Run `python -m app.cli.login`.")
        return 1

    return run_login(auth, open_browser=not args.no_browser)


if __name__ == "__main__":
    sys.exit(main())
