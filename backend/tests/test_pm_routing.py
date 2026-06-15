"""Tests for PM first-pass routing (implementation-plan T4.3)."""

from __future__ import annotations

import pytest

from app.roles.envelope import Action, Role
from app.roles.pm import format_delivery, route_inbound
from app.storage.db import connect
from app.storage.migrations import migrate
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
