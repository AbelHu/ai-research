"""Characterization tests for config loading (design-spec §7, §13).

Covers `load_models_config` (valid file + the shipped config) and
`ModelsConfig.provider_for_role` (valid role, unknown role, and a role that
points at an undefined provider). These lock the role->provider contract that
the advisor wrapper depends on.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from app.config.settings import (
    DEFAULT_MODELS_CONFIG,
    ModelsConfig,
    ProviderConfig,
    load_models_config,
)

VALID_YAML = """\
roles:
  triage: fast
  planner: quality
providers:
  fast:
    kind: github_models
    model: openai/gpt-4o-mini
    api_key_env: GITHUB_MODELS_TOKEN
  quality:
    kind: openai_compatible
    model: gpt-4o
    base_url: https://api.openai.com/v1
    api_key_env: OPENAI_API_KEY
"""

# A role ("planner") points at a provider name that is never defined.
DANGLING_PROVIDER_YAML = """\
roles:
  planner: does_not_exist
providers:
  fast:
    kind: github_models
    model: openai/gpt-4o-mini
"""


def _write(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "models.yaml"
    path.write_text(text, encoding="utf-8")
    return path


def test_load_models_config_valid(tmp_path: Path) -> None:
    cfg = load_models_config(_write(tmp_path, VALID_YAML))
    assert isinstance(cfg, ModelsConfig)
    assert cfg.roles["triage"] == "fast"
    assert cfg.providers["quality"].base_url == "https://api.openai.com/v1"


def test_shipped_models_config_loads() -> None:
    # The config that actually ships with the repo must parse.
    cfg = load_models_config(DEFAULT_MODELS_CONFIG)
    assert cfg.roles, "shipped models.yaml should define at least one role"
    for role, provider_name in cfg.roles.items():
        assert provider_name in cfg.providers, (
            f"role {role!r} maps to undefined provider {provider_name!r}"
        )


def test_provider_for_role_valid(tmp_path: Path) -> None:
    cfg = load_models_config(_write(tmp_path, VALID_YAML))
    provider = cfg.provider_for_role("triage")
    assert isinstance(provider, ProviderConfig)
    assert provider.kind == "github_models"
    assert provider.model == "openai/gpt-4o-mini"


def test_provider_for_role_unknown_role(tmp_path: Path) -> None:
    cfg = load_models_config(_write(tmp_path, VALID_YAML))
    with pytest.raises(KeyError):
        cfg.provider_for_role("nope")


def test_provider_for_role_dangling_provider(tmp_path: Path) -> None:
    cfg = load_models_config(_write(tmp_path, DANGLING_PROVIDER_YAML))
    with pytest.raises(KeyError):
        cfg.provider_for_role("planner")


def test_load_models_config_rejects_missing_fields(tmp_path: Path) -> None:
    # `providers` is required; omitting it is a schema error.
    bad = "roles:\n  triage: fast\n"
    with pytest.raises(ValidationError):
        load_models_config(_write(tmp_path, bad))
