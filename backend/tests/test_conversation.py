"""Tests for the conversation-context builder (design-spec §6C continuity)."""

from __future__ import annotations

import pytest

from app.roles import conversation
from app.roles.envelope import Action, Role, RoleMessage
from app.storage.db import connect
from app.storage.migrations import migrate
from app.storage.repos import identities as identities_repo
from app.storage.repos import requests as requests_repo
from app.storage.repos import role_messages as role_messages_repo


@pytest.fixture
def conn():
    c = connect()
    migrate(c)
    try:
        yield c
    finally:
        c.close()


def _record_answer(conn, request_id: int, job_id: int, answer_text: str) -> None:
    role_messages_repo.record_envelope(
        conn,
        RoleMessage(
            request_id=request_id,
            job_id=job_id,
            from_role=Role.boss,
            to_role=Role.pm,
            action=Action.deliver,
            payload={"answer": {"answer": answer_text, "citations": []}},
        ),
    )


def test_get_last_answer_text_returns_most_recent(conn) -> None:
    req = requests_repo.create_request(conn, title="gold price")
    job = requests_repo.create_job(conn, request_id=req.id, kind="ask")
    assert role_messages_repo.get_last_answer_text(conn, req.id) is None
    _record_answer(conn, req.id, job.id, "first answer")
    _record_answer(conn, req.id, job.id, "the gold price is at https://goldapi.io")
    assert "goldapi.io" in role_messages_repo.get_last_answer_text(conn, req.id)


def test_get_latest_active_request_excludes_given(conn) -> None:
    user_id = identities_repo.ensure_owner(conn)
    a = requests_repo.create_request(conn, title="first", user_id=user_id)
    b = requests_repo.create_request(conn, title="second", user_id=user_id)
    assert requests_repo.get_latest_active_request(conn, user_id).id == b.id
    # Excluding the newest yields the prior turn.
    assert (
        requests_repo.get_latest_active_request(conn, user_id, exclude_request_id=b.id).id == a.id
    )


def test_load_returns_none_without_user_or_prior(conn) -> None:
    assert conversation.load(conn, None) is None
    user_id = identities_repo.ensure_owner(conn)
    assert conversation.load(conn, user_id) is None  # no requests yet


def test_build_and_render_includes_answer_and_plan(conn) -> None:
    user_id = identities_repo.ensure_owner(conn)
    req = requests_repo.create_request(conn, title="implement a gold-price skill", user_id=user_id)
    job = requests_repo.create_job(conn, request_id=req.id, kind="feature")
    _record_answer(conn, req.id, job.id, "Here is the gold price URL: https://goldapi.io")
    # A plan with two phases (so 'provide me the plan' can be grounded).
    from app.advisor.schemas import PhaseSpec, PlanSpec, TaskSpec
    from app.storage.repos import plans as plans_repo

    spec = PlanSpec(
        phases=[
            PhaseSpec(title="Research the API", tasks=[TaskSpec(title="read docs")]),
            PhaseSpec(title="Write the skill", tasks=[TaskSpec(title="codegen")]),
        ]
    )
    plans_repo.create_plan_from_spec(conn, job_id=job.id, spec=spec)

    ctx = conversation.load(conn, user_id)
    assert ctx is not None
    assert ctx.plan_outline == ["Research the API", "Write the skill"]

    rendered = conversation.render(ctx)
    assert "previous turn" in rendered
    assert "implement a gold-price skill" in rendered
    assert "goldapi.io" in rendered
    assert "Research the API" in rendered
    assert "Write the skill" in rendered
    # Renders nothing for an empty context.
    assert conversation.render(None) == ""


def test_render_truncates_long_answer(conn) -> None:
    user_id = identities_repo.ensure_owner(conn)
    req = requests_repo.create_request(conn, title="x", user_id=user_id)
    job = requests_repo.create_job(conn, request_id=req.id, kind="ask")
    _record_answer(conn, req.id, job.id, "y" * 1000)
    rendered = conversation.render(conversation.load(conn, user_id))
    assert "…" in rendered
    assert "y" * 1000 not in rendered
