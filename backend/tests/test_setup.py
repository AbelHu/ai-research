"""Tests for the setup wizard orchestration + CLI (implementation-plan T9.6).

Offline: `run_setup` / `check` are driven with injected I/O + seams. Verifies the
full configure run, the pure-skip re-run (asks nothing), `--check` reporting, and
`--reconfigure`.
"""

from __future__ import annotations

import textwrap

import pytest

from app.cli.setup import check, run_setup
from app.setup.config_writer import ROUTE_COPILOT, ROUTE_MODELS, EnvFile, current_route
from app.setup.prompts import Prompter
from app.setup.steps import KEPT, MISSING
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


class _Raises:
    """A reader that fails if the wizard tries to prompt (asserts 'asks nothing')."""

    def __call__(self, prompt: str) -> str:  # pragma: no cover - only on failure
        raise AssertionError(f"unexpected prompt: {prompt!r}")


class _Script:
    def __init__(self, answers):
        self._answers = list(answers)

    def __call__(self, prompt: str) -> str:
        return self._answers.pop(0) if self._answers else ""


class _FakeAuth:
    def __init__(self, logged_in):
        self._logged_in = logged_in

    def is_logged_in(self):
        return self._logged_in


@pytest.fixture
def conn():
    c = connect()
    migrate(c)
    try:
        yield c
    finally:
        c.close()


@pytest.fixture
def models_path(tmp_path):
    path = tmp_path / "models.yaml"
    path.write_text(MODELS_COPILOT, encoding="utf-8")
    return path


def test_run_setup_pure_skip_asks_nothing(conn, tmp_path, models_path) -> None:
    # Everything already configured: copilot logged in, telegram token, an account paired.
    env_path = tmp_path / ".env"
    env = EnvFile("TELEGRAM_BOT_TOKEN=existing\nTAVILY_API_KEY=tvly-existing\n")
    owner_id = identities_repo.ensure_owner(conn)
    identities_repo.bind_identity(
        conn, user_id=owner_id, channel="telegram", channel_user_id="42", paired_via="host_code"
    )

    out: list[str] = []
    prompter = Prompter(reader=_Raises(), secret_reader=_Raises(), writer=out.append)
    dry_run_calls = []

    rc = run_setup(
        conn,
        prompter,
        env,
        models_path,
        env_path,
        dry_run_fn=lambda: dry_run_calls.append("x") or 0,
        provider_kwargs={"auth": _FakeAuth(True), "getenv": lambda _k: None},
    )

    assert rc == 0
    assert dry_run_calls == ["x"]  # verify still runs
    # All four steps were kept (the summary says so), and nothing was asked.
    summary = "\n".join(out)
    assert summary.count("configured (kept)") == 4


def test_run_setup_configures_everything(conn, tmp_path, models_path) -> None:
    env_path = tmp_path / ".env"
    env = EnvFile("")

    # provider: choose B + PAT; telegram: token; web search: Tavily key. Pairing
    # is informational (no prompt).
    prompter = Prompter(
        reader=_Script(["B"]),
        secret_reader=_Script(["ghp_pat", "123:tok", "tvly-key"]),
        writer=lambda _m: None,
    )

    rc = run_setup(
        conn,
        prompter,
        env,
        models_path,
        env_path,
        dry_run_fn=lambda: 0,
        provider_kwargs={"auth": _FakeAuth(False), "getenv": lambda _k: None},
        telegram_kwargs={"verify_fn": lambda t: (True, "mybot")},
    )

    assert rc == 0
    # .env persisted with all captured secrets.
    saved = EnvFile.load(env_path)
    assert saved.get("GITHUB_MODELS_TOKEN") == "ghp_pat"
    assert saved.get("TELEGRAM_BOT_TOKEN") == "123:tok"
    assert saved.get("TAVILY_API_KEY") == "tvly-key"
    # models.yaml switched to the PAT route; the owner record exists (no GitHub login).
    assert current_route(models_path) == ROUTE_MODELS
    assert identities_repo.get_owner(conn) is not None


def test_run_setup_returns_verify_exit_code(conn, tmp_path, models_path) -> None:
    env = EnvFile("TELEGRAM_BOT_TOKEN=x\nTAVILY_API_KEY=tvly-x\n")
    identities_repo.set_owner_github_login(conn, "octocat")
    prompter = Prompter(reader=_Raises(), secret_reader=_Raises(), writer=lambda _m: None)
    rc = run_setup(
        conn,
        prompter,
        env,
        models_path,
        tmp_path / ".env",
        dry_run_fn=lambda: 1,  # verify failed
        provider_kwargs={"auth": _FakeAuth(True), "getenv": lambda _k: None},
    )
    assert rc == 1


# --- check (no writes, no prompts) ------------------------------------------


def test_check_all_missing(conn, models_path) -> None:
    results = check(conn, EnvFile(""), models_path, auth=_FakeAuth(False), getenv=lambda _k: None)
    by_name = {r.name: r for r in results}
    assert by_name["AI provider"].status == MISSING
    assert by_name["Telegram"].status == MISSING
    # Pairing is request-and-approve at runtime — informational, never a blocker.
    assert by_name["Pairing"].status == KEPT
    # Web search is optional — KEPT (off) without a key, never a blocker.
    assert by_name["Web search"].status == KEPT
    assert "off" in by_name["Web search"].detail


def test_check_web_search_present_with_key(conn, models_path) -> None:
    env = EnvFile("TAVILY_API_KEY=tvly-x\n")
    results = check(conn, env, models_path, auth=_FakeAuth(False), getenv=lambda _k: None)
    by_name = {r.name: r for r in results}
    assert by_name["Web search"].status == KEPT
    assert "present" in by_name["Web search"].detail


def test_check_all_configured(conn, models_path) -> None:
    identities_repo.set_owner_github_login(conn, "octocat")
    env = EnvFile("TELEGRAM_BOT_TOKEN=x\n")
    results = check(conn, env, models_path, auth=_FakeAuth(True), getenv=lambda _k: None)
    assert all(r.status == KEPT for r in results)


def test_check_does_not_write(conn, tmp_path, models_path) -> None:
    before = models_path.read_text(encoding="utf-8")
    env = EnvFile("")
    check(conn, env, models_path, auth=_FakeAuth(False), getenv=lambda _k: None)
    assert models_path.read_text(encoding="utf-8") == before  # unchanged


# --- reconfigure ------------------------------------------------------------


def test_reconfigure_reasks_provider(conn, tmp_path, models_path) -> None:
    # Provider is usable (logged in) but --reconfigure=provider forces a re-ask.
    env = EnvFile("TELEGRAM_BOT_TOKEN=x\n")
    identities_repo.set_owner_github_login(conn, "octocat")
    login_calls = []
    prompter = Prompter(
        reader=_Script(["A"]),  # choose Route A again
        secret_reader=_Script([]),
        writer=lambda _m: None,
    )
    rc = run_setup(
        conn,
        prompter,
        env,
        models_path,
        tmp_path / ".env",
        reconfigure="provider",
        dry_run_fn=lambda: 0,
        provider_kwargs={
            "auth": _FakeAuth(True),
            "login_fn": lambda a, p: login_calls.append("x") or True,
            "getenv": lambda _k: None,
        },
    )
    assert rc == 0
    assert login_calls == ["x"]  # re-asked + re-logged-in despite being usable
    assert current_route(models_path) == ROUTE_COPILOT
