"""Setup wizard steps (implementation-plan T9.3-T9.5).

Each step **detects existing configuration and skips by default** (never
clobbering a working machine), reports a `StepResult`, and only prompts to fill a
gap. The external seams (device-flow login, Telegram ``getMe``) are **injectable**
so the steps are unit-tested fully offline; the defaults wire the real
implementations. Chat pairing is request-and-approve at runtime (no GitHub), so
its step is purely informational.
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


# --- T9.5: chat pairing -----------------------------------------------------


def pairing_step(
    conn: sqlite3.Connection,
    prompter: Prompter,
    *,
    reconfigure: bool = False,
) -> StepResult:
    """Explain chat pairing + ensure the owner record exists (§10.1).

    Pairing is **request-and-approve at runtime** — no GitHub account, no login.
    Once the bot runs, a user messages it, the bot replies a one-time code, and
    the operator runs ``pair --approve <code>`` on the trusted console to admit
    them. So there's nothing to authenticate here: the step just creates the
    owner record that paired accounts attach to and shows what to do next.
    """
    from app.storage.repos import identities as identities_repo

    paired = identities_repo.list_identities(conn, state="paired")
    if not reconfigure and paired:
        return StepResult("Pairing", KEPT, f"{len(paired)} account(s) paired")

    identities_repo.ensure_owner(conn)
    prompter.say("Chat pairing (request-and-approve — no account or login needed):")
    prompter.say("  1. Start the bot:    python -m app.cli.telegram")
    prompter.say("  2. Message the bot from your chat app — it replies a one-time code.")
    prompter.say("  3. Approve it here:  python -m app.cli.pair --approve <code>")
    prompter.say("     The bot then accepts that account's messages (repeat per account).")
    return StepResult("Pairing", CONFIGURED, "request-and-approve at runtime")
