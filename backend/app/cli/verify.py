"""Verify GitHub Models login / configuration (design spec O13, section 7.2).

Run from the `backend/` directory:

    python -m app.cli.verify            # default: config-only, no network
    python -m app.cli.verify --dry-run  # explicit config-only check (CI-safe)
    python -m app.cli.verify --live     # also run a live catalog + completion

The default and ``--dry-run`` modes never touch the network: they validate the
role->provider mapping and that required API-key environment variables are
present. ``--live`` additionally performs a real catalog lookup and a tiny
completion so you can confirm the account works. No secrets are printed.
"""

from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Callable

import httpx
from dotenv import load_dotenv

from app.advisor.providers import (
    GITHUB_MODELS_API_VERSION,
    GITHUB_MODELS_HOST,
    CompletionRequest,
    MissingCredentialError,
    build_provider,
)
from app.config.settings import (
    REPO_ROOT,
    ModelsConfig,
    ProviderConfig,
    load_models_config,
)
from app.security import Secret

Getenv = Callable[[str], "str | None"]

CATALOG_URL = f"{GITHUB_MODELS_HOST}/catalog/models"


def _print_header() -> None:
    print("GitHub Models - login / configuration check")
    print("=" * 44)


def _list_catalog(token: Secret, limit: int = 5) -> None:
    """Best-effort: list a few available models to confirm auth works."""
    try:
        with httpx.Client(timeout=30.0) as client:
            resp = client.get(
                CATALOG_URL,
                headers={
                    "Authorization": f"Bearer {token.reveal()}",
                    "Accept": "application/vnd.github+json",
                    "X-GitHub-Api-Version": GITHUB_MODELS_API_VERSION,
                },
            )
        if resp.status_code == 200:
            data = resp.json()
            items = data if isinstance(data, list) else data.get("models", [])
            names = [(m.get("id") or m.get("name")) for m in items if isinstance(m, dict)]
            shown = ", ".join(n for n in names[:limit] if n) or "(none returned)"
            print(f"[ok]   Catalog reachable. Sample models: {shown}")
        else:
            print(f"[warn] Catalog request returned HTTP {resp.status_code}.")
    except httpx.HTTPError as exc:
        print(f"[warn] Could not reach catalog endpoint: {exc}")


def _print_role_mapping(models: ModelsConfig) -> None:
    print("Role -> provider mapping:")
    for role, provider_name in models.roles.items():
        provider = models.providers.get(provider_name)
        model = provider.model if provider else "?"
        print(f"  - {role:<9} -> {provider_name} ({model})")
    print()


def _referenced_providers(models: ModelsConfig) -> list[ProviderConfig]:
    """Providers actually used by at least one role (deduplicated)."""
    seen: dict[str, ProviderConfig] = {}
    for provider_name in models.roles.values():
        provider = models.providers.get(provider_name)
        if provider is not None:
            seen[provider_name] = provider
    return list(seen.values())


def _check_config(models: ModelsConfig, getenv: Getenv) -> list[str]:
    """Return a list of config problems (empty == OK). Never touches network."""
    problems: list[str] = []

    # 1. Every role must map to a defined provider.
    for role, provider_name in models.roles.items():
        if provider_name not in models.providers:
            problems.append(f"role {role!r} maps to undefined provider {provider_name!r}")

    # 2. Each referenced provider's API-key env var (if any) must be present.
    needed_envs = sorted({p.api_key_env for p in _referenced_providers(models) if p.api_key_env})
    for env_var in needed_envs:
        if getenv(env_var):
            print(f"[ok]   {env_var} is set.")
        else:
            problems.append(f"environment variable {env_var} is not set")

    # 3. Route A (github_copilot) needs a device-flow login, not an env var.
    if any(p.kind == "github_copilot" for p in _referenced_providers(models)):
        from app.advisor.auth import GitHubCopilotAuth

        if GitHubCopilotAuth().is_logged_in():
            print("[ok]   GitHub Copilot login found (device-flow token cached).")
        else:
            problems.append(
                "a github_copilot provider is configured but you're not logged in "
                "(run `python -m app.cli.login`)"
            )

    return problems


def run_dry_run(models: ModelsConfig, getenv: Getenv) -> int:
    """Config-only check (no network). Exit 0 when the config is valid."""
    _print_role_mapping(models)
    problems = _check_config(models, getenv)
    if problems:
        for problem in problems:
            print(f"[fail] {problem}")
        print()
        print("[fail] Config check failed (dry-run, no network).")
        return 1
    print("[ok]   Every role maps to a defined provider.")
    print()
    print("[done] Dry-run passed (config valid, no network).")
    return 0


def run_live(models: ModelsConfig, getenv: Getenv) -> int:
    """Full check: validate config, then a live catalog lookup and completion."""
    _print_role_mapping(models)
    problems = _check_config(models, getenv)
    if problems:
        for problem in problems:
            print(f"[fail] {problem}")
        print()
        print("[fail] Fix the configuration before running the live check.")
        return 1

    token = getenv("GITHUB_MODELS_TOKEN")
    if token:
        _list_catalog(Secret(token))

    # Run a tiny completion through the 'fast' role (cheapest) end-to-end.
    try:
        provider_cfg = models.provider_for_role("fast")
        provider = build_provider(provider_cfg, getenv=getenv)
        print(f"[..]   Testing a completion with {provider.model} ...")
        resp = provider.complete(
            CompletionRequest(
                messages=[{"role": "user", "content": "Reply with the single word: pong"}],
                max_tokens=5,
            )
        )
        reply = resp.text.strip() or "(empty response)"
        print(f"[ok]   Model replied: {reply!r}")
    except MissingCredentialError as exc:
        print(f"[fail] {exc}")
        return 1
    except httpx.HTTPStatusError as exc:
        code = exc.response.status_code
        # `exc` now carries the provider's (redacted) error body — print it.
        print(f"[fail] {exc}")
        if code in (401, 403):
            print("       Token may be invalid or missing the 'models: read' permission,")
            print("       or GitHub Models is not enabled for your enterprise/org.")
        elif code == 429:
            print("       Rate limited. Wait and retry, or opt into paid usage / BYOK.")
        elif code == 400:
            print("       The model rejected a request field. Common causes:")
            print("       - the model only allows the default temperature (e.g. reasoning models),")
            print("       - it needs 'max_completion_tokens' rather than 'max_tokens', or")
            print("       - it doesn't support response_format json_object.")
        return 1
    except httpx.HTTPError as exc:
        print(f"[fail] Network error during completion: {exc}")
        return 1

    print()
    print("[done] GitHub Models is configured correctly (live).")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m app.cli.verify",
        description="Verify AI provider configuration (config-only by default).",
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--dry-run",
        action="store_true",
        help="config-only check, no network (default)",
    )
    mode.add_argument(
        "--live",
        action="store_true",
        help="also run a live catalog lookup and completion (network)",
    )
    args = parser.parse_args(argv)

    _print_header()
    # Populate the environment from .env for humans, without overriding any
    # variable already set (so tests and CI stay in control).
    load_dotenv(REPO_ROOT / ".env", override=False)

    try:
        models = load_models_config()
    except (OSError, ValueError) as exc:
        print(f"[fail] Could not load models config: {exc}")
        return 1

    if args.live:
        return run_live(models, os.getenv)
    return run_dry_run(models, os.getenv)


if __name__ == "__main__":
    sys.exit(main())
