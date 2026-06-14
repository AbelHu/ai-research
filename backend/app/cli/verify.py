"""Verify GitHub Models login / configuration (design spec O13, section 7.2).

Run from the `backend/` directory:

    python -m app.cli.verify

It checks that your token is present, optionally lists a few catalog models,
and runs a tiny completion so you can confirm the account is configured.
No secrets are printed.
"""

from __future__ import annotations

import sys

import httpx

from app.advisor.providers import (
    GITHUB_MODELS_API_VERSION,
    GITHUB_MODELS_HOST,
    CompletionRequest,
    MissingCredentialError,
    build_provider,
)
from app.config.settings import get_settings, load_models_config

CATALOG_URL = f"{GITHUB_MODELS_HOST}/catalog/models"


def _print_header() -> None:
    print("GitHub Models - login / configuration check")
    print("=" * 44)


def _list_catalog(token: str, limit: int = 5) -> None:
    """Best-effort: list a few available models to confirm auth works."""
    try:
        with httpx.Client(timeout=30.0) as client:
            resp = client.get(
                CATALOG_URL,
                headers={
                    "Authorization": f"Bearer {token}",
                    "Accept": "application/vnd.github+json",
                    "X-GitHub-Api-Version": GITHUB_MODELS_API_VERSION,
                },
            )
        if resp.status_code == 200:
            data = resp.json()
            items = data if isinstance(data, list) else data.get("models", [])
            names = [
                (m.get("id") or m.get("name"))
                for m in items
                if isinstance(m, dict)
            ]
            shown = ", ".join(n for n in names[:limit] if n) or "(none returned)"
            print(f"[ok]   Catalog reachable. Sample models: {shown}")
        else:
            print(f"[warn] Catalog request returned HTTP {resp.status_code}.")
    except httpx.HTTPError as exc:
        print(f"[warn] Could not reach catalog endpoint: {exc}")


def main() -> int:
    _print_header()
    settings = get_settings()
    models = load_models_config()

    # Show how roles map to providers/models.
    print("Role -> provider mapping:")
    for role, provider_name in models.roles.items():
        provider = models.providers.get(provider_name)
        model = provider.model if provider else "?"
        print(f"  - {role:<9} -> {provider_name} ({model})")
    print()

    if not settings.github_models_token:
        print("[fail] GITHUB_MODELS_TOKEN is not set.")
        print()
        print("To fix:")
        print("  1. Copy .env.example to .env")
        print("  2. Create a fine-grained PAT with the 'models: read' permission")
        print("     (GitHub -> Settings -> Developer settings -> Fine-grained tokens)")
        print("  3. Set GITHUB_MODELS_TOKEN=<your token> in .env")
        print("  4. (optional) Set GITHUB_ORG=<your-org> for org-attributed usage")
        print("  5. Re-run: python -m app.cli.verify")
        return 1

    if settings.github_org:
        print(f"[ok]   Org-attributed endpoint enabled for org: {settings.github_org}")
    else:
        print("[info] No GITHUB_ORG set - using the personal inference endpoint.")

    _list_catalog(settings.github_models_token)

    # Run a tiny completion through the 'fast' role (cheapest) end-to-end.
    try:
        provider_cfg = models.provider_for_role("fast")
        provider = build_provider(provider_cfg)
        print(f"[..]   Testing a completion with {provider.model} ...")
        resp = provider.complete(
            CompletionRequest(
                messages=[
                    {"role": "user", "content": "Reply with the single word: pong"}
                ],
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
        print(f"[fail] Completion request failed with HTTP {code}.")
        if code in (401, 403):
            print("       Token may be invalid or missing the 'models: read' permission,")
            print("       or GitHub Models is not enabled for your enterprise/org.")
        elif code == 429:
            print("       Rate limited. Wait and retry, or opt into paid usage / BYOK.")
        return 1
    except httpx.HTTPError as exc:
        print(f"[fail] Network error during completion: {exc}")
        return 1

    print()
    print("[done] GitHub Models is configured correctly.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
