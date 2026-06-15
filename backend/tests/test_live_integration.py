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
    # Meaningful: the model actually *followed the instruction* — not merely that
    # it returned something non-empty.
    assert "pong" in resp.text.strip().lower()
    assert resp.model  # the provider reports a model id


# --- the advisor contracts, validated against the real model ----------------


def test_live_triage_validates(conn, resolver) -> None:
    req = requests_repo.create_request(conn, title="2+2")
    advisor = _advisor(conn, resolver)

    result = advisor.triage("what is 2+2?", request_id=req.id)

    # "what is 2+2?" is a maximally-clear factual question: a competent model must
    # classify it as a clear, simple ask. We assert the *actual classification*,
    # not merely that the output fits the enum — a wrong (or fallback)
    # classification fails here, which is what makes this a real test. (The
    # fallback would read ask/unclear/complex, so this also pins it to a genuine
    # model reply.)
    assert (result.kind, result.clarity, result.complexity) == ("ask", "clear", "simple")
    row = ai_calls_repo.list_ai_calls(conn, req.id)[0]
    assert row["validation_status"] in ("valid", "repaired")


def test_live_analyze_validates(conn, resolver) -> None:
    req = requests_repo.create_request(conn, title="compare vendors")
    advisor = _advisor(conn, resolver)

    analysis = advisor.analyze(
        text=(
            "Compare AWS, Azure and GCP for hosting a new web app, weigh the "
            "trade-offs, and recommend one with justification."
        ),
        request_id=req.id,
    )

    row = ai_calls_repo.list_ai_calls(conn, req.id)[0]
    assert row["validation_status"] in ("valid", "repaired")
    # Multi-step research + weigh trade-offs + recommend = complex work, not a
    # one-shot factual ask. That's the semantic signal that should route the
    # platform onto the plan path, so it's the meaningful thing to assert (and it
    # came from a genuine reply, per the validation-status check above).
    assert analysis.complexity == "complex"
    assert isinstance(analysis.belongs, bool)
    assert analysis.kind in ("ask", "task", "feature")


def test_live_answer_cites_a_source(conn, resolver) -> None:
    req = requests_repo.create_request(conn, title="capital of France")
    advisor = _advisor(conn, resolver)

    draft = advisor.answer(
        text="What is the capital of France? Answer from the provided context.",
        hits=[{"ref": "m1", "content": "The capital of France is Paris."}],
        request_id=req.id,
    )

    # Meaningful: the answer is actually *correct* and *grounded in the source we
    # handed the model* — it cites our memory ref `m1`, not an invented one.
    assert "paris" in draft.answer.lower()
    assert any(c.ref == "m1" for c in draft.citations), draft.citations


def test_live_answer_grounds_in_provided_context(conn, resolver) -> None:
    """The core anti-hallucination guarantee: the model answers from *our* context,
    not its training — and **selects the right item** rather than echoing.

    We feed several made-up facts the model cannot know (release codenames) and
    ask about one of them. To pass, the model must (a) return the codename only
    the provided context contains, (b) *not* leak the distractor codenames, and
    (c) cite the **specific** source ref it used (``m2``), ignoring the others.
    A model that merely parrots the request, or answers from training, cannot do
    this — which is what makes it a meaningful grounding test.
    """
    req = requests_repo.create_request(conn, title="release codename")
    advisor = _advisor(conn, resolver)

    draft = advisor.answer(
        text="What is the internal codename for the Q3 release? Use the provided context.",
        hits=[
            {"ref": "m1", "content": "The Q2 release is codenamed 'Brass Otter'."},
            {"ref": "m2", "content": "The Q3 release is codenamed 'Marmalade Falcon'."},
            {"ref": "m4", "content": "The Q4 release is codenamed 'Velvet Heron'."},
        ],
        request_id=req.id,
    )

    answer = draft.answer.lower()
    # Picked the correct fact for *Q3*...
    assert "marmalade falcon" in answer
    # ...and didn't leak the distractor codenames (Q2 / Q4).
    assert "brass otter" not in answer
    assert "velvet heron" not in answer
    # ...and cited the specific source it used, not the distractors.
    cited = {c.ref for c in draft.citations}
    assert "m2" in cited, draft.citations
    assert not (cited & {"m1", "m4"}), draft.citations


# --- the headline: a simple ask, end-to-end, through the real model ---------


def test_live_end_to_end_ask(conn, resolver) -> None:
    memories_repo.create_memory(conn, content="The capital of France is Paris.")
    advisor = _advisor(conn, resolver)
    user_id = ensure_owner(conn)

    outcome = run_ask(conn, advisor, "What is the capital of France?", user_id=user_id)

    # A maximally-clear ask, answered end-to-end (not clarified/planned), with the
    # *correct* answer surfaced to the user and grounded in the seeded memory.
    assert outcome.status == "answered", f"unexpected outcome: {outcome.status}"
    assert outcome.answer is not None
    assert "paris" in outcome.answer.answer.lower()
    assert any(c.ref == "m1" for c in outcome.answer.citations), outcome.answer.citations
    assert outcome.delivery and "paris" in outcome.delivery.lower()
    assert f"/req {outcome.request.code}" in outcome.delivery
