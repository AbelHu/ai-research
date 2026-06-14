"""Tests for the offline `verify --dry-run` mode (implementation-plan T0.8).

These run with no network (the autouse `_no_network` guard would raise if any
code path tried to reach out). They lock that:
  * a valid config with the required token present exits 0,
  * a missing token or a dangling provider exits 1,
  * the default (no-flag) CLI invocation is offline and exits 0.
"""

from __future__ import annotations

import pytest

from app.cli import verify
from app.config.settings import ModelsConfig

_VALID = ModelsConfig.model_validate(
    {
        "roles": {"fast": "gh"},
        "providers": {
            "gh": {
                "kind": "github_models",
                "model": "openai/gpt-4o-mini",
                "api_key_env": "GITHUB_MODELS_TOKEN",
            }
        },
    }
)

_DANGLING = ModelsConfig.model_validate(
    {
        "roles": {"fast": "missing"},
        "providers": {"gh": {"kind": "github_models", "model": "m"}},
    }
)


def test_run_dry_run_ok_with_token() -> None:
    rc = verify.run_dry_run(_VALID, {"GITHUB_MODELS_TOKEN": "dummy"}.get)
    assert rc == 0


def test_run_dry_run_fails_without_token() -> None:
    rc = verify.run_dry_run(_VALID, lambda _name: None)
    assert rc == 1


def test_run_dry_run_fails_on_dangling_provider() -> None:
    rc = verify.run_dry_run(_DANGLING, {"GITHUB_MODELS_TOKEN": "dummy"}.get)
    assert rc == 1


def test_main_default_is_offline_and_zero(monkeypatch: pytest.MonkeyPatch) -> None:
    # Default (no flag) and explicit --dry-run must be offline and exit 0
    # when the required token is present. Uses the shipped models.yaml.
    monkeypatch.setattr(verify, "load_dotenv", lambda *a, **k: None)
    monkeypatch.setenv("GITHUB_MODELS_TOKEN", "dummy")
    assert verify.main([]) == 0
    assert verify.main(["--dry-run"]) == 0


def test_main_live_short_circuits_without_token(monkeypatch: pytest.MonkeyPatch) -> None:
    # --live must validate config first; a missing token exits 1 *before* any
    # network call (the _no_network guard would otherwise raise).
    monkeypatch.setattr(verify, "load_dotenv", lambda *a, **k: None)
    monkeypatch.setattr(verify, "load_models_config", lambda: _VALID)
    monkeypatch.delenv("GITHUB_MODELS_TOKEN", raising=False)
    assert verify.main(["--live"]) == 1
