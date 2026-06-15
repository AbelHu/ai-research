"""Tests for the RoleMessage envelope + repo round-trip (implementation-plan T4.1)."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.roles.envelope import Action, Role, RoleMessage
from app.storage.db import connect
from app.storage.migrations import migrate
from app.storage.repos import requests as requests_repo
from app.storage.repos import role_messages as repo


@pytest.fixture
def db():
    conn = connect()
    migrate(conn)
    req = requests_repo.create_request(conn)
    try:
        yield conn, req.id
    finally:
        conn.close()


def test_envelope_action_is_constrained() -> None:
    with pytest.raises(ValidationError):
        RoleMessage(
            request_id=1,
            from_role=Role.pm,
            to_role=Role.boss,
            action="not_a_verb",  # not in the Action vocabulary
        )


def test_envelope_rejects_unknown_field() -> None:
    with pytest.raises(ValidationError):
        RoleMessage(
            request_id=1,
            from_role=Role.pm,
            to_role=Role.boss,
            action=Action.route_request,
            surprise="nope",
        )


def test_record_and_round_trip(db) -> None:
    conn, request_id = db
    msg = RoleMessage(
        request_id=request_id,
        from_role=Role.pm,
        to_role=Role.boss,
        action=Action.route_request,
        payload={"text": "hello", "append": False},
        template="pm.route@v1",
    )
    msg_id = repo.record_envelope(conn, msg)

    back = repo.envelope_from_row(repo.get_role_message(conn, msg_id))
    assert back.id == msg_id
    assert back.request_id == request_id
    assert back.from_role is Role.pm
    assert back.to_role is Role.boss
    assert back.action is Action.route_request
    assert back.payload == {"text": "hello", "append": False}
    assert back.template == "pm.route@v1"
    assert back.status == "queued"
    assert back.created_at is not None


def test_causation_chain(db) -> None:
    conn, request_id = db
    first = repo.record_envelope(
        conn,
        RoleMessage(
            request_id=request_id,
            from_role=Role.pm,
            to_role=Role.boss,
            action=Action.route_request,
        ),
    )
    second = repo.record_envelope(
        conn,
        RoleMessage(
            request_id=request_id,
            from_role=Role.boss,
            to_role=Role.analyzer,
            action=Action.analyze,
        ),
        causation_id=first,
    )
    chain = repo.list_role_messages(conn, request_id)
    assert [r["id"] for r in chain] == [first, second]
    assert repo.get_role_message(conn, second)["causation_id"] == first


def test_update_status(db) -> None:
    conn, request_id = db
    msg_id = repo.record_envelope(
        conn,
        RoleMessage(
            request_id=request_id,
            from_role=Role.boss,
            to_role=Role.junior,
            action=Action.answer_ask,
        ),
    )
    repo.update_status(conn, msg_id, "done")
    assert repo.get_role_message(conn, msg_id)["status"] == "done"
