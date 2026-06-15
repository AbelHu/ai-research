"""Tests for the setup config writer (implementation-plan T9.1).

Pure + offline: exercises the `.env` editor (preserve/round-trip/idempotent) and
the `config/models.yaml` provider-route switch (in-place, comment-preserving).
"""

from __future__ import annotations

import textwrap

import pytest

from app.setup.config_writer import (
    ROUTE_COPILOT,
    ROUTE_MODELS,
    EnvFile,
    current_route,
    set_provider_route,
)

# A models.yaml mirroring the shipped file's shape (Route A for fast/quality).
SHIPPED_MODELS = textwrap.dedent(
    """\
    # Model role -> provider mapping.
    roles:
      triage: fast
      planner: quality
      drafter: quality
      embedder: embed

    providers:
      fast:
        kind: github_copilot             # Route A
        model: gpt-4o-mini               # bare model id

      quality:
        kind: github_copilot
        model: gpt-4o

      embed:
        # Copilot has no embeddings endpoint.
        kind: github_models
        model: openai/text-embedding-3-small
        api_key_env: GITHUB_MODELS_TOKEN
        org_env: GITHUB_ORG
    """
)


# --- EnvFile ----------------------------------------------------------------


def test_set_new_key_appends() -> None:
    env = EnvFile("")
    env.set("TELEGRAM_BOT_TOKEN", "abc123")
    assert env.dumps() == "TELEGRAM_BOT_TOKEN=abc123\n"


def test_set_existing_key_updates_in_place_no_duplicate() -> None:
    env = EnvFile("A=1\nB=2\nC=3\n")
    env.set("B", "changed")
    out = env.dumps()
    assert out == "A=1\nB=changed\nC=3\n"
    assert out.count("B=") == 1  # idempotent, never duplicated


def test_preserves_comments_blank_lines_and_order() -> None:
    text = "# header\n\nGITHUB_MODELS_TOKEN=\n# trailing note\nGITHUB_ORG=acme\n"
    env = EnvFile(text)
    env.set("GITHUB_MODELS_TOKEN", "ghp_x")
    out = env.dumps()
    assert out == "# header\n\nGITHUB_MODELS_TOKEN=ghp_x\n# trailing note\nGITHUB_ORG=acme\n"


def test_get_and_has_value() -> None:
    env = EnvFile("EMPTY=\nFULL=value\n")
    assert env.get("FULL") == "value"
    assert env.get("EMPTY") == ""
    assert env.get("MISSING") is None
    assert env.has_value("FULL") is True
    assert env.has_value("EMPTY") is False  # present but blank -> not configured
    assert env.has_value("MISSING") is False


def test_load_missing_file_is_empty(tmp_path) -> None:
    env = EnvFile.load(tmp_path / "nope.env")
    assert env.dumps() == ""


def test_save_and_reload_round_trips(tmp_path) -> None:
    path = tmp_path / ".env"
    env = EnvFile("A=1\n# note\n")
    env.set("B", "2")
    env.save(path)
    again = EnvFile.load(path)
    assert again.get("A") == "1"
    assert again.get("B") == "2"
    assert "# note" in again.dumps()


# --- models.yaml provider route --------------------------------------------


def test_current_route_reads_drafter_provider(tmp_path) -> None:
    path = tmp_path / "models.yaml"
    path.write_text(SHIPPED_MODELS, encoding="utf-8")
    assert current_route(path) == ROUTE_COPILOT


def test_switch_to_models_route_updates_fast_and_quality(tmp_path) -> None:
    path = tmp_path / "models.yaml"
    path.write_text(SHIPPED_MODELS, encoding="utf-8")

    changed = set_provider_route(path, ROUTE_MODELS)
    assert changed is True
    assert current_route(path) == ROUTE_MODELS

    out = path.read_text(encoding="utf-8")
    # fast + quality flipped to github_models with a PAT env + publisher-prefixed ids.
    assert "model: openai/gpt-4o-mini" in out
    assert "model: openai/gpt-4o" in out
    assert out.count("api_key_env: GITHUB_MODELS_TOKEN") == 3  # fast, quality, embed
    # The embed block + the roles section + a comment are all preserved.
    assert "model: openai/text-embedding-3-small" in out
    assert "drafter: quality" in out
    assert "# Model role -> provider mapping." in out


def test_switch_route_is_idempotent(tmp_path) -> None:
    path = tmp_path / "models.yaml"
    path.write_text(SHIPPED_MODELS, encoding="utf-8")
    assert set_provider_route(path, ROUTE_COPILOT) is False  # already Route A → no change
    set_provider_route(path, ROUTE_MODELS)
    assert set_provider_route(path, ROUTE_MODELS) is False  # second apply → no change


def test_switch_back_to_copilot_drops_api_key_env(tmp_path) -> None:
    path = tmp_path / "models.yaml"
    path.write_text(SHIPPED_MODELS, encoding="utf-8")
    set_provider_route(path, ROUTE_MODELS)
    set_provider_route(path, ROUTE_COPILOT)
    out = path.read_text(encoding="utf-8")
    assert current_route(path) == ROUTE_COPILOT
    # Only the embed block keeps the PAT env now (fast/quality dropped it).
    assert out.count("api_key_env: GITHUB_MODELS_TOKEN") == 1


def test_unknown_route_raises(tmp_path) -> None:
    path = tmp_path / "models.yaml"
    path.write_text(SHIPPED_MODELS, encoding="utf-8")
    with pytest.raises(ValueError):
        set_provider_route(path, "nonsense")
