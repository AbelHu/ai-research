"""Tests for the requests/jobs repository (implementation-plan T1.9)."""

from __future__ import annotations

from datetime import datetime

import pytest

from app.storage.db import connect
from app.storage.migrations import migrate
from app.storage.repos import requests as repo


@pytest.fixture
def conn():
    c = connect()
    migrate(c)
    try:
        yield c
    finally:
        c.close()


def test_first_code_has_no_suffix(conn) -> None:
    now = datetime(2026, 6, 14, 12, 0, 0)
    req = repo.create_request(conn, title="first", now=now)
    assert req.code == "20260614120000"


def test_same_second_collision_gets_suffix(conn) -> None:
    now = datetime(2026, 6, 14, 12, 0, 0)
    first = repo.create_request(conn, title="first", now=now)
    second = repo.create_request(conn, title="second", now=now)
    third = repo.create_request(conn, title="third", now=now)
    assert first.code == "20260614120000"
    assert second.code == "20260614120000-01"
    assert third.code == "20260614120000-02"


def test_different_seconds_no_suffix(conn) -> None:
    a = repo.create_request(conn, now=datetime(2026, 6, 14, 12, 0, 0))
    b = repo.create_request(conn, now=datetime(2026, 6, 14, 12, 0, 1))
    assert a.code == "20260614120000"
    assert b.code == "20260614120001"


def test_get_and_get_by_code(conn) -> None:
    req = repo.create_request(conn, title="hello", now=datetime(2026, 6, 14, 12, 0, 0))
    assert repo.get_request(conn, req.id) == req
    assert repo.get_request_by_code(conn, req.code) == req
    assert repo.get_request(conn, 9999) is None


def test_request_job_link_round_trip(conn) -> None:
    req = repo.create_request(conn, title="t", now=datetime(2026, 6, 14, 12, 0, 0))
    job = repo.create_job(conn, request_id=req.id, kind="task", complexity="high")
    assert job.request_id == req.id
    assert job.kind == "task"
    assert job.paused is False
    assert repo.get_job_for_request(conn, req.id) == job


def test_list_requests_filters_by_state_newest_first(conn) -> None:
    a = repo.create_request(conn, now=datetime(2026, 6, 14, 12, 0, 0))
    b = repo.create_request(conn, now=datetime(2026, 6, 14, 12, 0, 1))
    # Archive the first one.
    conn.execute("UPDATE requests SET state = 'archived' WHERE id = ?", (a.id,))
    conn.commit()

    active = repo.list_requests(conn, state="active")
    assert [r.id for r in active] == [b.id]
    everything = repo.list_requests(conn)
    assert [r.id for r in everything] == [b.id, a.id]  # newest first


def test_improves_request_link(conn) -> None:
    origin = repo.create_request(conn, now=datetime(2026, 6, 14, 12, 0, 0))
    improvement = repo.create_request(
        conn, improves_request_id=origin.id, now=datetime(2026, 6, 14, 12, 0, 1)
    )
    assert improvement.improves_request_id == origin.id
