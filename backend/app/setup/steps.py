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
    ROUTE_OLLAMA,
    ROUTE_OPENAI,
    EnvFile,
    api_key_env_for,
    current_api_key_env,
    current_route,
    route_model_defaults,
    set_custom_provider,
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


def _provider_usable(
    route: str | None,
    env: EnvFile,
    auth,
    getenv: Getenv,
    *,
    api_key_env: str | None = None,
) -> bool:
    """Whether the configured route is already usable (skip-existing detection)."""
    if route == ROUTE_COPILOT:
        return bool(auth and auth.is_logged_in())
    if route == ROUTE_MODELS:
        return env.has_value("GITHUB_MODELS_TOKEN") or bool(getenv("GITHUB_MODELS_TOKEN"))
    if route == ROUTE_OLLAMA:
        return True  # local endpoint — no credential needed
    if route == ROUTE_OPENAI:
        # A custom endpoint is usable when its API-key env var holds a value, or
        # when it declares no key at all (e.g. an unauthenticated local server).
        if not api_key_env:
            return True
        return env.has_value(api_key_env) or bool(getenv(api_key_env))
    return False


def _default_login(auth, prompter: Prompter) -> bool:
    """Drive the real device-flow login (no browser auto-open); return success."""
    from app.cli.login import run_login

    return run_login(auth, open_browser=False) == 0


def _choose_models(
    prompter: Prompter,
    *,
    fast_default: str | None = None,
    quality_default: str | None = None,
) -> tuple[str, str] | None:
    """Prompt for the **fast** + **quality** model ids (the app's two model tiers).

    ``fast`` backs quick/cheap roles (triage, extraction); ``quality`` backs
    planning + drafting. Enter keeps the shown default. For a custom endpoint no
    default is offered, so the fast model is required (blank → ``None``) and the
    quality model defaults to the fast one (single-model endpoints just Enter).
    """
    prompter.say("  Models (Enter keeps the default):")
    fast = prompter.ask("    fast model (quick, cheap tasks)", default=fast_default).strip()
    fast = fast or (fast_default or "")
    if not fast:
        return None
    quality = prompter.ask(
        "    quality model (planning, drafting)", default=quality_default or fast
    ).strip()
    quality = quality or quality_default or fast
    return fast, quality


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
    if not reconfigure and _provider_usable(
        route, env, auth, getenv, api_key_env=current_api_key_env(models_path)
    ):
        return StepResult("AI provider", KEPT, f"route={route}")

    prompter.say("AI provider:")
    prompter.say("  A) GitHub Copilot — sign in with your GitHub account (device flow, no PAT)")
    prompter.say("  B) GitHub Models — paste a fine-grained PAT (models: read)")
    prompter.say(
        "  C) Other — any OpenAI-compatible endpoint (OpenAI, Azure, OpenRouter, Ollama, …)"
    )
    choice = prompter.ask("Choose A, B or C", default="A").strip().upper()

    if choice.startswith("B"):
        token = prompter.secret("GitHub Models PAT", current=env.get("GITHUB_MODELS_TOKEN") or None)
        if not token:
            return StepResult("AI provider", MISSING, "no PAT entered")
        env.set("GITHUB_MODELS_TOKEN", token)
        defaults = route_model_defaults(ROUTE_MODELS)
        chosen = _choose_models(
            prompter, fast_default=defaults["fast"], quality_default=defaults["quality"]
        )
        assert chosen is not None  # defaults provided → never None
        fast_model, quality_model = chosen
        set_provider_route(
            models_path, ROUTE_MODELS, fast_model=fast_model, quality_model=quality_model
        )
        return StepResult("AI provider", CONFIGURED, f"route=github_models ({quality_model})")

    if choice.startswith("C"):
        return _configure_custom_provider(prompter, env, models_path)

    # Route A: device-flow login, then point fast/quality at github_copilot.
    if not login_fn(auth, prompter):
        return StepResult("AI provider", MISSING, "device-flow login did not complete")
    defaults = route_model_defaults(ROUTE_COPILOT)
    chosen = _choose_models(
        prompter, fast_default=defaults["fast"], quality_default=defaults["quality"]
    )
    assert chosen is not None  # defaults provided → never None
    fast_model, quality_model = chosen
    set_provider_route(
        models_path, ROUTE_COPILOT, fast_model=fast_model, quality_model=quality_model
    )
    return StepResult("AI provider", CONFIGURED, f"route=github_copilot ({quality_model})")


def _configure_custom_provider(prompter: Prompter, env: EnvFile, models_path: Path) -> StepResult:
    """Route C: configure a BYO OpenAI-compatible / Ollama provider from prompts.

    The API key (if any) is written to ``.env`` under a derived name and only
    that **name** is recorded in ``models.yaml`` (secrets never inline, §12).
    Both the ``fast`` and ``quality`` roles point at this single endpoint; the
    ``embedder`` is left as-is (swap it by hand if your endpoint embeds too).
    """
    from app.advisor.providers import DEFAULT_API_MODE, SUPPORTED_API_MODES

    name = prompter.ask("Provider name (e.g. openrouter, azure, local)", default="custom").strip()
    name = name or "custom"
    adapter = prompter.ask(
        "Provider type (openai-compatible | ollama)", default="openai-compatible"
    )
    kind = ROUTE_OLLAMA if adapter.strip().lower().startswith("ollama") else ROUTE_OPENAI

    default_base = "http://localhost:11434/v1" if kind == ROUTE_OLLAMA else None
    base_url = prompter.ask("Base URL", default=default_base).strip()
    if not base_url:
        return StepResult("AI provider", MISSING, "no base URL entered")
    chosen = _choose_models(prompter)  # no defaults: fast required, quality → fast
    if chosen is None:
        return StepResult("AI provider", MISSING, "no model entered")
    fast_model, quality_model = chosen

    # api_mode = chat request protocol. chat_completions is the default + the
    # only one wired today; an unsupported value warns and falls back (never
    # blocks setup) so the file is always left in a runnable state.
    api_mode = prompter.ask("API mode", default=DEFAULT_API_MODE).strip() or DEFAULT_API_MODE
    if api_mode not in SUPPORTED_API_MODES:
        supported = ", ".join(sorted(SUPPORTED_API_MODES))
        prompter.say(
            f"  ! api_mode {api_mode!r} isn't supported yet — using {DEFAULT_API_MODE} "
            f"(supported: {supported})."
        )
        api_mode = DEFAULT_API_MODE

    api_key_env: str | None = None
    if kind == ROUTE_OPENAI:
        api_key_env = api_key_env_for(name)
        key = prompter.secret(
            f"API key (saved to .env as {api_key_env}; blank if none)",
            current=env.get(api_key_env) or None,
        )
        if key:
            env.set(api_key_env, key)
        elif not env.has_value(api_key_env):
            api_key_env = None  # keyless endpoint (e.g. local server without auth)

    set_custom_provider(
        models_path,
        kind=kind,
        fast_model=fast_model,
        quality_model=quality_model,
        base_url=base_url,
        api_mode=api_mode,
        api_key_env=api_key_env,
    )
    return StepResult("AI provider", CONFIGURED, f"route={kind} ({name}, {api_mode})")


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
