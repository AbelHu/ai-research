"""Tests for the setup wizard steps (implementation-plan T9.3-T9.5).

Offline: every external seam (device-flow login, Telegram getMe, owner
establishment, code minting) is injected. Covers the skip-existing path and the
configure path for each step.
"""

from __future__ import annotations

import textwrap

import pytest

from app.setup.config_writer import ROUTE_COPILOT, ROUTE_MODELS, EnvFile, current_route
from app.setup.prompts import Prompter
from app.setup.steps import (
    CONFIGURED,
    KEPT,
    MISSING,
    pairing_step,
    provider_step,
    telegram_step,
)
from app.storage.db import connect
from app.storage.migrations import migrate
from app.storage.repos import identities as identities_repo

MODELS_COPILOT = textwrap.dedent(
    """\
    roles:
      drafter: quality
      embedder: embed
    providers:
      fast:
        kind: github_copilot
        model: gpt-4o-mini
      quality:
        kind: github_copilot
        model: gpt-4o
      embed:
        kind: github_models
        model: openai/text-embedding-3-small
        api_key_env: GITHUB_MODELS_TOKEN
    """
)


class _ScriptReader:
    def __init__(self, answers):
        self._answers = list(answers)

    def __call__(self, prompt: str) -> str:
        return self._answers.pop(0) if self._answers else ""


def _prompter(answers=(), secrets=()):
    out: list[str] = []
    return (
        Prompter(
            reader=_ScriptReader(answers),
            secret_reader=_ScriptReader(secrets),
            writer=out.append,
        ),
        out,
    )


class _FakeAuth:
    def __init__(self, logged_in: bool) -> None:
        self._logged_in = logged_in

    def is_logged_in(self) -> bool:
        return self._logged_in


@pytest.fixture
def models_path(tmp_path):
    path = tmp_path / "models.yaml"
    path.write_text(MODELS_COPILOT, encoding="utf-8")
    return path


@pytest.fixture
def conn():
    c = connect()
    migrate(c)
    try:
        yield c
    finally:
        c.close()


# --- T9.3 provider ----------------------------------------------------------


def test_provider_kept_when_copilot_already_logged_in(models_path) -> None:
    prompter, _ = _prompter()
    result = provider_step(
        prompter, EnvFile(""), models_path, auth=_FakeAuth(True), getenv=lambda _k: None
    )
    assert result.status == KEPT
    assert "github_copilot" in result.detail


def test_provider_route_a_logs_in_and_sets_route(models_path) -> None:
    prompter, _ = _prompter(answers=["A"])
    calls = []
    result = provider_step(
        prompter,
        EnvFile(""),
        models_path,
        auth=_FakeAuth(False),
        login_fn=lambda auth, p: calls.append("login") or True,
        getenv=lambda _k: None,
    )
    assert result.status == CONFIGURED
    assert calls == ["login"]
    assert current_route(models_path) == ROUTE_COPILOT


def test_provider_route_b_captures_pat_and_switches(models_path) -> None:
    prompter, _ = _prompter(answers=["B"], secrets=["ghp_pat"])
    env = EnvFile("")
    result = provider_step(
        prompter, env, models_path, auth=_FakeAuth(False), getenv=lambda _k: None
    )
    assert result.status == CONFIGURED
    assert env.get("GITHUB_MODELS_TOKEN") == "ghp_pat"
    assert current_route(models_path) == ROUTE_MODELS


def test_provider_route_b_without_pat_is_missing(models_path) -> None:
    prompter, _ = _prompter(answers=["B"], secrets=[""])
    result = provider_step(
        prompter, EnvFile(""), models_path, auth=_FakeAuth(False), getenv=lambda _k: None
    )
    assert result.status == MISSING


def test_provider_reconfigure_forces_prompt_even_when_usable(models_path) -> None:
    prompter, _ = _prompter(answers=["A"])
    result = provider_step(
        prompter,
        EnvFile(""),
        models_path,
        auth=_FakeAuth(True),
        login_fn=lambda auth, p: True,
        reconfigure=True,
        getenv=lambda _k: None,
    )
    assert result.status == CONFIGURED  # re-asked despite being logged in


# --- T9.4 telegram ----------------------------------------------------------


def test_telegram_kept_when_token_present() -> None:
    prompter, _ = _prompter()
    result = telegram_step(prompter, EnvFile("TELEGRAM_BOT_TOKEN=existing\n"))
    assert result.status == KEPT


def test_telegram_captures_and_verifies() -> None:
    prompter, _ = _prompter(secrets=["123:abc"])
    env = EnvFile("")
    result = telegram_step(prompter, env, verify_fn=lambda t: (True, "mybot"))
    assert result.status == CONFIGURED
    assert "mybot" in result.detail
    assert env.get("TELEGRAM_BOT_TOKEN") == "123:abc"


def test_telegram_saves_even_when_verify_fails() -> None:
    prompter, out = _prompter(secrets=["123:abc"])
    env = EnvFile("")
    result = telegram_step(prompter, env, verify_fn=lambda t: (False, ""))
    assert result.status == CONFIGURED
    assert "unverified" in result.detail
    assert env.get("TELEGRAM_BOT_TOKEN") == "123:abc"


def test_telegram_skip_verify_writes_without_call() -> None:
    prompter, _ = _prompter(secrets=["123:abc"])
    called = []
    result = telegram_step(
        prompter,
        EnvFile(""),
        verify_fn=lambda t: called.append(t) or (True, "x"),
        skip_verify=True,
    )
    assert result.status == CONFIGURED
    assert called == []  # verify never invoked


def test_telegram_no_token_is_missing() -> None:
    prompter, _ = _prompter(secrets=[""])
    result = telegram_step(prompter, EnvFile(""))
    assert result.status == MISSING


# --- T9.5 chat pairing (informational, request-and-approve, no GitHub) -------


def test_pairing_creates_owner_and_shows_instructions(conn) -> None:
    prompter, out = _prompter()  # informational: asks nothing
    result = pairing_step(conn, prompter)
    assert result.status == CONFIGURED
    # The owner record now exists; no GitHub login is involved.
    assert identities_repo.get_owner(conn) is not None
    # The next-step instructions were shown to the user.
    text = "\n".join(out)
    assert "pair --approve" in text
    assert "app.cli.telegram" in text


def test_pairing_kept_when_accounts_paired(conn) -> None:
    owner_id = identities_repo.ensure_owner(conn)
    identities_repo.bind_identity(
        conn, user_id=owner_id, channel="telegram", channel_user_id="42", paired_via="host_code"
    )
    prompter, _ = _prompter()  # KEPT path asks nothing
    result = pairing_step(conn, prompter)
    assert result.status == KEPT
    assert "1 account" in result.detail


def test_pairing_reconfigure_shows_instructions_again(conn) -> None:
    owner_id = identities_repo.ensure_owner(conn)
    identities_repo.bind_identity(
        conn, user_id=owner_id, channel="telegram", channel_user_id="42", paired_via="host_code"
    )
    prompter, out = _prompter()
    result = pairing_step(conn, prompter, reconfigure=True)
    assert result.status == CONFIGURED
    assert "pair --approve" in "\n".join(out)
