"""Setup wizard steps (implementation-plan T9.3-T9.5).

Each step **detects existing configuration and skips by default** (never
clobbering a working machine), reports a `StepResult`, and only prompts to fill a
gap. Every external seam (device-flow login, Telegram ``getMe``, owner
establishment, code minting) is **injectable** so the steps are unit-tested fully
offline; the defaults wire the real implementations.
"""

from __future__ import annotations

import os
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from app.setup.config_writer import (
    ROUTE_COPILOT,
    ROUTE_MODELS,
    EnvFile,
    current_route,
    set_provider_route,
)
from app.setup.prompts import Prompter

# Step outcomes (the plan's "configured (kept)" / "set up now" / "still missing").
KEPT = "kept"
CONFIGURED = "configured"
MISSING = "missing"

Getenv = Callable[[str], "str | None"]


@dataclass(frozen=True)
class StepResult:
    name: str
    status: str  # KEPT | CONFIGURED | MISSING
    detail: str = ""

    @property
    def ok(self) -> bool:
        """Whether the step ended configured (either kept or freshly set up)."""
        return self.status in (KEPT, CONFIGURED)


# --- T9.3: AI provider ------------------------------------------------------


def _provider_usable(route: str | None, env: EnvFile, auth, getenv: Getenv) -> bool:
    """Whether the configured route is already usable (skip-existing detection)."""
    if route == ROUTE_COPILOT:
        return bool(auth and auth.is_logged_in())
    if route == ROUTE_MODELS:
        return env.has_value("GITHUB_MODELS_TOKEN") or bool(getenv("GITHUB_MODELS_TOKEN"))
    return False


def _default_login(auth, prompter: Prompter) -> bool:
    """Drive the real device-flow login (no browser auto-open); return success."""
    from app.cli.login import run_login

    return run_login(auth, open_browser=False) == 0


def provider_step(
    prompter: Prompter,
    env: EnvFile,
    models_path: Path,
    *,
    auth=None,
    login_fn: Callable[[object, Prompter], bool] = _default_login,
    reconfigure: bool = False,
    getenv: Getenv = os.getenv,
) -> StepResult:
    """Configure + authenticate the AI provider (Route A Copilot / Route B PAT)."""
    if auth is None:
        from app.advisor.auth import GitHubCopilotAuth

        auth = GitHubCopilotAuth()

    route = current_route(models_path)
    if not reconfigure and _provider_usable(route, env, auth, getenv):
        return StepResult("AI provider", KEPT, f"route={route}")

    prompter.say("AI provider:")
    prompter.say("  A) GitHub Copilot — sign in with your GitHub account (device flow, no PAT)")
    prompter.say("  B) GitHub Models — paste a fine-grained PAT (models: read)")
    choice = prompter.ask("Choose A or B", default="A").strip().upper()

    if choice.startswith("B"):
        token = prompter.secret("GitHub Models PAT", current=env.get("GITHUB_MODELS_TOKEN") or None)
        if not token:
            return StepResult("AI provider", MISSING, "no PAT entered")
        env.set("GITHUB_MODELS_TOKEN", token)
        set_provider_route(models_path, ROUTE_MODELS)
        return StepResult("AI provider", CONFIGURED, "route=github_models")

    # Route A: device-flow login, then point fast/quality at github_copilot.
    if not login_fn(auth, prompter):
        return StepResult("AI provider", MISSING, "device-flow login did not complete")
    set_provider_route(models_path, ROUTE_COPILOT)
    return StepResult("AI provider", CONFIGURED, "route=github_copilot")


# --- T9.4: Telegram ---------------------------------------------------------


def _default_verify_telegram(token: str) -> tuple[bool, str]:
    """Call ``getMe`` to confirm a token works; return (ok, bot_username)."""
    from app.channels.telegram import TelegramAdapter, TelegramError

    try:
        me = TelegramAdapter(token).get_me()
    except TelegramError:
        return False, ""
    return True, str(me.get("username") or "")


def telegram_step(
    prompter: Prompter,
    env: EnvFile,
    *,
    verify_fn: Callable[[str], tuple[bool, str]] = _default_verify_telegram,
    skip_verify: bool = False,
    reconfigure: bool = False,
) -> StepResult:
    """Capture (and optionally verify) the Telegram bot token."""
    current = env.get("TELEGRAM_BOT_TOKEN") or None
    if not reconfigure and current:
        return StepResult("Telegram", KEPT, "bot token present")

    prompter.say("Telegram: create a bot with @BotFather and paste its token (or skip).")
    token = prompter.secret("Telegram bot token", current=current)
    if not token:
        return StepResult("Telegram", MISSING, "no bot token entered")
    env.set("TELEGRAM_BOT_TOKEN", token)

    if skip_verify:
        return StepResult("Telegram", CONFIGURED, "saved (verify skipped)")
    ok, username = verify_fn(token)
    if not ok:
        prompter.say("  ! getMe failed — token saved, but double-check it later.")
        return StepResult("Telegram", CONFIGURED, "saved (unverified)")
    detail = f"verified as @{username}" if username else "verified"
    return StepResult("Telegram", CONFIGURED, detail)


# --- T9.5: owner pairing ----------------------------------------------------


def _default_establish_owner(conn: sqlite3.Connection, prompter: Prompter) -> str | None:
    """Run the real device-flow owner challenge; return the established login."""
    from app.advisor.auth import AuthError, GitHubCopilotAuth
    from app.gateway.pairing import establish_owner

    def on_prompt(device) -> None:
        prompter.say(f"  Open {device.verification_uri} and enter code: {device.user_code}")

    try:
        return establish_owner(conn, GitHubCopilotAuth(), on_prompt=on_prompt)
    except AuthError:
        return None


def _default_mint_code(conn: sqlite3.Connection) -> str:
    """Mint a host one-time pairing code; return the plaintext (shown once)."""
    from app.storage.repos import pairing_codes as pairing_codes_repo

    return pairing_codes_repo.mint_code(conn).code


def pairing_step(
    conn: sqlite3.Connection,
    prompter: Prompter,
    *,
    settings=None,
    establish_fn: Callable[[sqlite3.Connection, Prompter], str | None] = _default_establish_owner,
    mint_fn: Callable[[sqlite3.Connection], str] = _default_mint_code,
    reconfigure: bool = False,
) -> StepResult:
    """Establish the owner (device-flow) and/or mint a host ``/pair`` code (§10.1)."""
    from app.storage.repos import identities as identities_repo

    expected = identities_repo.expected_owner_login(conn, settings=settings)
    if not reconfigure and expected:
        # Pure skip: an established owner is left untouched and we ask nothing.
        # (Need a fresh chat code later? `python -m app.cli.pair`.)
        return StepResult("Owner pairing", KEPT, f"owner={expected}")

    if not prompter.confirm("Establish the owner now via GitHub device-flow?", default=True):
        return StepResult("Owner pairing", MISSING, "skipped")
    login = establish_fn(conn, prompter)
    if not login:
        return StepResult("Owner pairing", MISSING, "owner challenge did not complete")
    if prompter.confirm("Mint a pairing code for your chat account now?", default=True):
        code = mint_fn(conn)
        prompter.say(f"  Pairing code (send `/pair {code}` from your chat account): {code}")
    return StepResult("Owner pairing", CONFIGURED, f"owner={login}")
