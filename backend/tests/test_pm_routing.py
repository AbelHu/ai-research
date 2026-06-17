"""Tests for PM first-pass routing (implementation-plan T4.3)."""

from __future__ import annotations

import pytest

from app.advisor.schemas import Source
from app.roles.envelope import Action, Role
from app.roles.pm import format_delivery, route_inbound
from app.roles.pm import route_new as pm_route_new
from app.storage.db import connect
from app.storage.migrations import migrate
from app.storage.repos import identities as identities_repo
from app.storage.repos import requests as requests_repo


@pytest.fixture
def conn():
    c = connect()
    migrate(c)
    try:
        yield c
    finally:
        c.close()


def test_empty_queue_mints_new_request(conn) -> None:
    result = route_inbound(conn, "what is the capital of France?")

    assert result.append is False
    assert result.detail_id is None
    assert result.request.title == "what is the capital of France?"
    # Emits a route_request envelope to the Boss carrying the RequestCard.
    env = result.envelope
    assert env.from_role is Role.pm
    assert env.to_role is Role.boss
    assert env.action is Action.route_request
    assert env.payload["request_id"] == result.request.id
    assert env.payload["append"] is False
    assert env.payload["text"] == "what is the capital of France?"


def test_explicit_req_id_appends(conn) -> None:
    first = route_inbound(conn, "compare three vendors")
    code = first.request.code

    result = route_inbound(conn, f"/req {code} also include pricing")

    assert result.append is True
    assert result.request.id == first.request.id  # same request, not a new one
    assert result.text == "also include pricing"
    assert result.detail_id is not None
    # The detail was persisted under the addressed request.
    details = requests_repo.list_request_details(conn, first.request.id)
    assert [d["content"] for d in details] == ["also include pricing"]
    assert result.envelope.payload["append"] is True


def test_unknown_req_code_mints_new(conn) -> None:
    # No request with this code exists → treated as a new request (full text).
    result = route_inbound(conn, "/req 19990101000000 stray message")
    assert result.append is False
    assert result.request.code != "19990101000000"


def test_unprefixed_followup_threads_to_awaiting_request(conn) -> None:
    user_id = identities_repo.ensure_owner(conn)
    first = route_inbound(conn, "implement a gold-price skill", user_id=user_id)
    # The pipeline asked the user for more detail (clarify / declined plan).
    requests_repo.set_request_status(conn, first.request.id, requests_repo.AWAITING_STATUS)

    # A plain follow-up (no /req marker) attaches to the awaiting request.
    result = route_inbound(conn, "here is the extra detail you asked for", user_id=user_id)
    assert result.append is True
    assert result.request.id == first.request.id
    details = requests_repo.list_request_details(conn, first.request.id)
    assert [d["content"] for d in details] == ["here is the extra detail you asked for"]
    # The awaiting flag is cleared once the reply is received (turn handed back).
    assert requests_repo.get_request(conn, first.request.id).status is None


def test_unprefixed_followup_provisionally_appends_to_current_thread(conn) -> None:
    user_id = identities_repo.ensure_owner(conn)
    first = route_inbound(conn, "what is the capital of France?", user_id=user_id)
    # A later message best-guesses a continuation of the current thread: a
    # PROVISIONAL append (detail not yet persisted) the Analyzer must confirm.
    result = route_inbound(conn, "what about Spain?", user_id=user_id)
    assert result.append is True
    assert result.provisional is True
    assert result.request.id == first.request.id
    assert result.detail_id is None  # not persisted until `belongs` is confirmed
    # Nothing was written under the first request yet (the guess is provisional).
    assert requests_repo.list_request_details(conn, first.request.id) == []


def test_route_new_mints_fresh_request(conn) -> None:
    user_id = identities_repo.ensure_owner(conn)
    first = route_inbound(conn, "first question", user_id=user_id)
    fresh = pm_route_new(conn, "an unrelated new question", user_id=user_id)
    assert fresh.append is False
    assert fresh.provisional is False
    assert fresh.request.id != first.request.id


def test_title_is_truncated(conn) -> None:
    long_text = "x" * 200
    result = route_inbound(conn, long_text)
    assert len(result.request.title) <= 60


def test_long_title_truncation_boundary(conn) -> None:
    result = route_inbound(conn, "y" * 60 + " trailing words that overflow the title cap")
    assert result.request.title == "y" * 60


def test_format_delivery_tags_request(conn) -> None:
    result = route_inbound(conn, "hello there")
    msg = format_delivery(result.request, "Hi!")
    assert f"/req {result.request.code}" in msg
    assert "hello there" in msg  # title
    assert "Hi!" in msg
    assert "Sources:" not in msg  # no sources → no Sources block


def test_format_delivery_shows_source_urls(conn) -> None:
    result = route_inbound(conn, "explain go memory management")
    sources = [
        Source(
            ref="https://go.dev/doc/gc-guide",
            url="https://go.dev/doc/gc-guide",
            title="Go GC Guide",
        ),
        Source(ref="https://go.dev/ref/mem", url="https://go.dev/ref/mem"),
    ]
    msg = format_delivery(result.request, "Go uses a garbage collector.", sources=sources)

    # The answer and a Sources block with the actual URLs are surfaced to the user.
    assert "Go uses a garbage collector." in msg
    assert "Sources:" in msg
    assert "Go GC Guide — https://go.dev/doc/gc-guide" in msg
    assert "  - https://go.dev/ref/mem" in msg


def test_format_delivery_renders_memory_ref_without_url(conn) -> None:
    result = route_inbound(conn, "capital of France")
    sources = [Source(ref="m1", title="Geography note")]
    msg = format_delivery(result.request, "Paris.", sources=sources)

    # A non-URL (memory) citation falls back to its opaque ref.
    assert "Sources:" in msg
    assert "Geography note (m1)" in msg
