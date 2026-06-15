"""Opt-in LIVE integration tests against a **real** AI model.

These are the only tests that touch the network. They are:
  * **excluded from the default run** (marked ``integration``, deselected by the
    ``addopts`` in ``pyproject.toml``), so the normal offline suite is never
    affected; and
  * **skipped cleanly** when no provider token is configured — so even running
    them without a token is a skip, not a failure.

Run them explicitly after configuring a token (e.g. a GitHub Models PAT with
``models: read`` in ``.env`` or the environment):

    python -m pytest -m integration -q
    # or target one:
    python -m pytest -m integration tests/test_live_integration.py::test_live_end_to_end_ask -q

What they prove that the offline suite can't: the **real** model returns output
that survives our strict template-requirement validation (anti-hallucination),
and a simple ask flows end-to-end through the real provider.
"""

from __future__ import annotations

import os

import pytest
from dotenv import load_dotenv

from app.advisor.providers import CompletionRequest
from app.advisor.wrapper import Advisor
from app.config.settings import REPO_ROOT, ModelsConfig, load_models_config
from app.roles.control import ensure_owner, run_ask
from app.storage.db import connect
from app.storage.migrations import migrate
from app.storage.repos import ai_calls as ai_calls_repo
from app.storage.repos import memories as memories_repo
from app.storage.repos import requests as requests_repo

# Every test in this module is a live integration test.
pytestmark = pytest.mark.integration

# The model-roles these tests exercise (advisor.triage→triage, analyze→planner,
# answer→drafter); we only require tokens for the providers behind these.
_REQUIRED_ROLES = ("triage", "planner", "drafter")


def _models() -> ModelsConfig:
    """Load .env + the models config, skipping if the config is absent."""
    load_dotenv(REPO_ROOT / ".env", override=False)
    try:
        return load_models_config()
    except FileNotFoundError as exc:  # pragma: no cover - environment dependent
        pytest.skip(f"models config not found: {exc}")


def _skip_unless_configured(models: ModelsConfig) -> None:
    """Skip (don't fail) unless every required provider is actually usable.

    For PAT providers (`github_models` / `openai_compatible`) that means the
    API-key env var is set; for `github_copilot` (Route A) it means a device-flow
    login is cached (`python -m app.cli.login`). Either way an unconfigured
    environment **skips**, never fails.
    """
    needed_env: set[str] = set()
    needs_copilot_login = False
    for role in _REQUIRED_ROLES:
        provider_name = models.roles.get(role)
        provider = models.providers.get(provider_name) if provider_name else None
        if provider is None:
            pytest.skip(f"model-role {role!r} is not defined in models.yaml")
        if provider.kind == "github_copilot":
            needs_copilot_login = True
        elif provider.api_key_env:
            needed_env.add(provider.api_key_env)

    missing = sorted(env for env in needed_env if not os.getenv(env))
    if missing:
        pytest.skip("live model token(s) not set: " + ", ".join(missing))
    if needs_copilot_login:
        from app.advisor.auth import GitHubCopilotAuth

        if not GitHubCopilotAuth().is_logged_in():
            pytest.skip("github_copilot provider not logged in (run `python -m app.cli.login`)")


@pytest.fixture(scope="module")
def resolver():
    """A real role→provider resolver, or a clean skip if unconfigured."""
    from app.cli.ask import build_resolver

    models = _models()
    _skip_unless_configured(models)
    return build_resolver(models)


@pytest.fixture
def conn():
    c = connect()
    migrate(c)
    try:
        yield c
    finally:
        c.close()


def _advisor(conn, resolver) -> Advisor:
    # Disable cited-URL fetching: it's exercised offline; here we only want a
    # deterministic, network-light check of the real model's JSON conformance.
    return Advisor(resolve_provider=resolver, conn=conn, verify_citations=False)


# --- smoke: raw provider completion ----------------------------------------


def test_live_completion_smoke(resolver) -> None:
    provider = resolver("triage")
    resp = provider.complete(
        CompletionRequest(
            messages=[{"role": "user", "content": "Reply with the single word: pong"}],
            max_tokens=5,
        )
    )
    assert resp.text.strip() != ""
    assert resp.model  # the provider reports a model id


# --- the advisor contracts, validated against the real model ----------------


def test_live_triage_validates(conn, resolver) -> None:
    req = requests_repo.create_request(conn, title="2+2")
    advisor = _advisor(conn, resolver)

    result = advisor.triage("what is 2+2?", request_id=req.id)

    assert result.kind in ("ask", "task", "feature")
    # The real model's output passed our strict template-requirement gate
    # (not a fallback) — the meaningful, model-agnostic invariant.
    row = ai_calls_repo.list_ai_calls(conn, req.id)[0]
    assert row["validation_status"] in ("valid", "repaired")


def test_live_analyze_validates(conn, resolver) -> None:
    req = requests_repo.create_request(conn, title="compare vendors")
    advisor = _advisor(conn, resolver)

    analysis = advisor.analyze(
        text="Compare three cloud vendors and recommend one.", request_id=req.id
    )

    assert isinstance(analysis.belongs, bool)
    assert analysis.kind in ("ask", "task", "feature")
    row = ai_calls_repo.list_ai_calls(conn, req.id)[0]
    assert row["validation_status"] in ("valid", "repaired")


def test_live_answer_cites_a_source(conn, resolver) -> None:
    req = requests_repo.create_request(conn, title="capital of France")
    advisor = _advisor(conn, resolver)

    draft = advisor.answer(
        text="What is the capital of France? Answer from the provided context.",
        hits=[{"ref": "m1", "content": "The capital of France is Paris."}],
        request_id=req.id,
    )

    assert draft.answer.strip() != ""
    assert len(draft.citations) >= 1  # schema requires ≥1 citation


# --- the headline: a simple ask, end-to-end, through the real model ---------


def test_live_end_to_end_ask(conn, resolver) -> None:
    memories_repo.create_memory(conn, content="The capital of France is Paris.")
    advisor = _advisor(conn, resolver)
    user_id = ensure_owner(conn)

    outcome = run_ask(conn, advisor, "What is the capital of France?", user_id=user_id)

    # A maximally-clear ask should be answered end-to-end (not clarified/planned).
    assert outcome.status == "answered", f"unexpected outcome: {outcome.status}"
    assert outcome.answer is not None
    assert len(outcome.answer.citations) >= 1
    assert outcome.delivery and f"/req {outcome.request.code}" in outcome.delivery
