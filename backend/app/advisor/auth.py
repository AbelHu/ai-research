"""GitHub Copilot device-flow login + token exchange (design-spec §7.2; plan T7.1).

Route A: "log in with my GitHub account" without minting a PAT. The flow
(RFC 8628 OAuth device flow, modeled on Hermes' ``copilot_auth.py``):

  1. **Device code** — ``POST /login/device/code`` → ``user_code`` + verification URL.
  2. **Prompt** — show the user the code + ``https://github.com/login/device``.
  3. **Poll** — ``POST /login/oauth/access_token`` until the user approves → a raw
     GitHub OAuth token (``gho_…``); handle ``authorization_pending`` / ``slow_down``.
  4. **Exchange** — ``GET /copilot_internal/v2/token`` (header ``Authorization:
     token <gho_>``) → a **short-lived Copilot API token** ``{token, expires_at}``.
  5. The provider (T7.2) calls models with ``Authorization: Bearer <copilot_token>``.

Security (§12): the cached credentials live **only** in a git-ignored,
permission-restricted file (``data/.auth/github.json``, ``0600``) — never in
SQLite, logs, or the audit trail. In-memory the tokens are wrapped in `Secret`
so they can't leak through reprs/logs; ``reveal()`` is called only at the HTTP
boundary. The HTTP client is injectable so the whole flow is unit-tested offline
against mocked GitHub endpoints (no network).
"""

from __future__ import annotations

import json
import os
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import httpx

from app.config.settings import get_settings
from app.security import Secret

# --- GitHub endpoints (verified against the device-flow references) ---------
DEVICE_CODE_URL = "https://github.com/login/device/code"
ACCESS_TOKEN_URL = "https://github.com/login/oauth/access_token"
COPILOT_TOKEN_URL = "https://api.github.com/copilot_internal/v2/token"
COPILOT_API_BASE = "https://api.githubcopilot.com"
GITHUB_USER_URL = "https://api.github.com/user"

# Public OAuth client id used by the GitHub Copilot editor integrations for the
# device flow (no app registration / client secret needed). Overridable via env.
DEFAULT_CLIENT_ID = "Iv1.b507a08c87ecfe98"
DEVICE_SCOPE = "read:user"

# Editor/integration headers the Copilot endpoints expect.
EDITOR_VERSION = "vscode/1.95.0"
EDITOR_PLUGIN_VERSION = "copilot-chat/0.23.0"
COPILOT_INTEGRATION_ID = "vscode-chat"
USER_AGENT = "GitHubCopilotChat/0.23.0"

# Refresh the short-lived Copilot token this many seconds before it expires.
REFRESH_MARGIN_SECONDS = 120
# Env vars an already-present GitHub token may live in (convenience precedence).
TOKEN_ENV_PRECEDENCE = ("COPILOT_GITHUB_TOKEN", "GH_TOKEN", "GITHUB_TOKEN")

ClientFactory = Callable[[], httpx.Client]


class AuthError(RuntimeError):
    """Base class for device-flow / token-exchange failures."""


class AuthPending(AuthError):
    """The user hasn't approved the device code yet (keep polling)."""


class AuthSlowDown(AuthError):
    """The server asked us to poll more slowly (RFC 8628: add 5s)."""


class AuthExpired(AuthError):
    """The device code expired before the user approved it."""


class AuthDenied(AuthError):
    """The user denied the authorization request."""


class NotLoggedIn(AuthError):
    """No cached OAuth token — the user must run the login flow first."""


@dataclass(frozen=True)
class DeviceCode:
    """The device-flow challenge shown to the user."""

    device_code: str
    user_code: str
    verification_uri: str
    interval: int
    expires_in: int


@dataclass(frozen=True)
class CopilotToken:
    """A short-lived Copilot API token + its absolute expiry (epoch seconds)."""

    token: Secret
    expires_at: int


def default_cache_path() -> Path:
    """The git-ignored token-cache file under the configured data dir (§7.2)."""
    return get_settings().data_dir / ".auth" / "github.json"


class GitHubCopilotAuth:
    """Device-flow login, token exchange, and the cached-credential lifecycle."""

    def __init__(
        self,
        *,
        cache_path: Path | None = None,
        client_id: str | None = None,
        client_factory: ClientFactory = httpx.Client,
        now: Callable[[], float] = time.time,
    ) -> None:
        self.cache_path = cache_path or default_cache_path()
        self.client_id = client_id or os.getenv("GITHUB_COPILOT_CLIENT_ID") or DEFAULT_CLIENT_ID
        self._client_factory = client_factory
        self._now = now

    # --- step 1: device code ------------------------------------------------

    def request_device_code(self) -> DeviceCode:
        """Start the flow: request a device + user code (§7.2 step 1)."""
        with self._client_factory() as client:
            resp = client.post(
                DEVICE_CODE_URL,
                headers={"Accept": "application/json", "User-Agent": USER_AGENT},
                data={"client_id": self.client_id, "scope": DEVICE_SCOPE},
            )
            resp.raise_for_status()
            data = resp.json()
        return DeviceCode(
            device_code=data["device_code"],
            user_code=data["user_code"],
            verification_uri=data.get("verification_uri", "https://github.com/login/device"),
            interval=int(data.get("interval", 5)),
            expires_in=int(data.get("expires_in", 900)),
        )

    # --- step 3: poll for the OAuth token -----------------------------------

    def poll_once(self, device_code: str) -> str | None:
        """Poll once for the OAuth token.

        Returns the raw ``gho_…`` token on success, ``None`` while the user has
        not yet approved, and raises a specific `AuthError` on a terminal state.
        """
        with self._client_factory() as client:
            resp = client.post(
                ACCESS_TOKEN_URL,
                headers={"Accept": "application/json", "User-Agent": USER_AGENT},
                data={
                    "client_id": self.client_id,
                    "device_code": device_code,
                    "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
                },
            )
            resp.raise_for_status()
            data = resp.json()

        token = data.get("access_token")
        if token:
            return token
        error = data.get("error")
        if error == "authorization_pending":
            return None
        if error == "slow_down":
            raise AuthSlowDown(data.get("error_description", "slow down"))
        if error == "expired_token":
            raise AuthExpired(data.get("error_description", "device code expired"))
        if error == "access_denied":
            raise AuthDenied(data.get("error_description", "access denied"))
        raise AuthError(data.get("error_description") or f"unexpected response: {error!r}")

    def poll_for_oauth_token(
        self,
        device: DeviceCode,
        *,
        sleep: Callable[[float], None] = time.sleep,
    ) -> str:
        """Block (polling) until the user approves; return the ``gho_`` token.

        ``sleep`` is injectable so tests don't actually wait. Honors the
        server's ``slow_down`` by widening the interval (RFC 8628).
        """
        interval = float(device.interval)
        deadline = self._now() + device.expires_in
        while True:
            if self._now() >= deadline:
                raise AuthExpired("device code expired before approval")
            sleep(interval)
            try:
                token = self.poll_once(device.device_code)
            except AuthSlowDown:
                interval += 5
                continue
            if token is not None:
                return token

    # --- step 4: exchange for a Copilot token -------------------------------

    def exchange_for_copilot_token(self, oauth_token: str) -> CopilotToken:
        """Exchange a ``gho_`` OAuth token for a short-lived Copilot API token."""
        with self._client_factory() as client:
            resp = client.get(
                COPILOT_TOKEN_URL,
                headers={
                    "Authorization": f"token {oauth_token}",
                    "Accept": "application/json",
                    "Editor-Version": EDITOR_VERSION,
                    "Editor-Plugin-Version": EDITOR_PLUGIN_VERSION,
                    "User-Agent": USER_AGENT,
                },
            )
            if resp.status_code in (401, 403):
                raise AuthError("Copilot token exchange refused (is the account Copilot-entitled?)")
            resp.raise_for_status()
            data = resp.json()
        return CopilotToken(token=Secret(data["token"]), expires_at=int(data["expires_at"]))

    # --- owner identity (pairing, §10.1) ------------------------------------

    def fetch_github_login(self, oauth_token: str) -> str:
        """Read the GitHub ``login`` for a ``gho_`` token (``GET /user``).

        Used by the pairing flow to verify a chat user approved the device flow
        **as the owner** (§10.1). The token is used only for this check and
        discarded by the caller — never cached (the chat user isn't logging in
        for LLM use).
        """
        with self._client_factory() as client:
            resp = client.get(
                GITHUB_USER_URL,
                headers={
                    "Authorization": f"token {oauth_token}",
                    "Accept": "application/json",
                    "User-Agent": USER_AGENT,
                },
            )
            resp.raise_for_status()
            data = resp.json()
        login = data.get("login")
        if not login:
            raise AuthError("GitHub /user returned no login")
        return str(login)

    # --- cached-credential lifecycle ----------------------------------------

    def _read_cache(self) -> dict | None:
        if not self.cache_path.exists():
            return None
        with open(self.cache_path, encoding="utf-8") as fh:
            return json.load(fh)

    def _write_cache(self, data: dict) -> None:
        """Persist credentials to the git-ignored cache with ``0600`` perms (§12)."""
        directory = self.cache_path.parent
        directory.mkdir(parents=True, exist_ok=True)
        os.chmod(directory, 0o700)
        # Write then tighten perms before any secret bytes could be world-readable.
        fd = os.open(self.cache_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh)
        os.chmod(self.cache_path, 0o600)

    def save_oauth_token(self, oauth_token: str) -> None:
        """Store the durable ``gho_`` OAuth token (the login result)."""
        cache = self._read_cache() or {}
        cache["oauth_token"] = oauth_token
        cache.pop("copilot_token", None)
        cache.pop("copilot_expires_at", None)
        self._write_cache(cache)

    def is_logged_in(self) -> bool:
        """Whether a cached OAuth token (or an env token) is available."""
        if find_existing_oauth_token() is not None:
            return True
        cache = self._read_cache()
        return bool(cache and cache.get("oauth_token"))

    def logout(self) -> None:
        """Remove the cached credentials."""
        self.cache_path.unlink(missing_ok=True)

    def get_bearer(self) -> str:
        """Return a valid Copilot API token, exchanging/refreshing as needed.

        Used by `GitHubCopilotProvider` per request. Raises `NotLoggedIn` if no
        OAuth token is available (run ``python -m app.cli.login`` first).
        """
        cache = self._read_cache() or {}
        oauth_token = cache.get("oauth_token") or find_existing_oauth_token()
        if not oauth_token:
            raise NotLoggedIn("no GitHub token cached; run `python -m app.cli.login`")

        cached_token = cache.get("copilot_token")
        expires_at = cache.get("copilot_expires_at", 0)
        if cached_token and self._now() < expires_at - REFRESH_MARGIN_SECONDS:
            return cached_token

        fresh = self.exchange_for_copilot_token(oauth_token)
        cache["oauth_token"] = oauth_token
        cache["copilot_token"] = fresh.token.reveal()
        cache["copilot_expires_at"] = fresh.expires_at
        self._write_cache(cache)
        return fresh.token.reveal()


def find_existing_oauth_token(getenv: Callable[[str], str | None] = os.getenv) -> str | None:
    """Reuse an already-present GitHub token from the environment, if any (§7.2).

    Convenience precedence mirroring the Copilot CLI; lets a user who already
    has ``GH_TOKEN`` / ``GITHUB_TOKEN`` set skip the device flow. Classic
    ``ghp_`` tokens are not supported by the Copilot exchange, so they're
    ignored here.
    """
    for env_var in TOKEN_ENV_PRECEDENCE:
        value = getenv(env_var)
        if value and not value.startswith("ghp_"):
            return value
    return None
