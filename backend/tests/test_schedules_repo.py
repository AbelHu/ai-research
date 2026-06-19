"""Tests for the schedules repository (design-spec §9, §11)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.storage.db import connect
from app.storage.migrations import migrate
from app.storage.repos import schedules as schedules_repo

NOW = datetime(2026, 6, 19, 12, 0, tzinfo=timezone.utc)


@pytest.fixture
def conn():
    c = connect()
    migrate(c)
    try:
        yield c
    finally:
        c.close()


def test_create_and_get_round_trips_params(conn) -> None:
    s = schedules_repo.create_schedule(
        conn, kind="memory_maintenance", schedule_cron="@daily", params={"foo": 1}
    )
    assert s.id and s.kind == "memory_maintenance" and s.enabled is True
    assert s.params == {"foo": 1}
    fetched = schedules_repo.get_schedule(conn, s.id)
    assert fetched is not None and fetched.params == {"foo": 1}


def test_get_by_kind_singleton(conn) -> None:
    assert schedules_repo.get_by_kind(conn, "memory_maintenance") is None
    schedules_repo.create_schedule(conn, kind="memory_maintenance", schedule_cron="@daily")
    assert schedules_repo.get_by_kind(conn, "memory_maintenance") is not None


def test_list_due_respects_next_run_and_enabled(conn) -> None:
    past, future = NOW - timedelta(hours=1), NOW + timedelta(hours=1)
    due = schedules_repo.create_schedule(conn, kind="a", schedule_cron="@daily", next_run_at=past)
    not_due = schedules_repo.create_schedule(
        conn, kind="b", schedule_cron="@daily", next_run_at=future
    )
    never = schedules_repo.create_schedule(conn, kind="c", schedule_cron="@daily")  # next_run None
    disabled = schedules_repo.create_schedule(
        conn, kind="d", schedule_cron="@daily", next_run_at=past, enabled=False
    )

    due_ids = {s.id for s in schedules_repo.list_due(conn, now=NOW)}
    assert due.id in due_ids
    assert never.id in due_ids  # never run → due
    assert not_due.id not in due_ids
    assert disabled.id not in due_ids


def test_mark_run_advances_next_run(conn) -> None:
    s = schedules_repo.create_schedule(
        conn, kind="a", schedule_cron="@daily", next_run_at=NOW - timedelta(hours=1)
    )
    schedules_repo.mark_run(conn, s.id, last_run_at=NOW, next_run_at=NOW + timedelta(days=1))
    got = schedules_repo.get_schedule(conn, s.id)
    assert got is not None and got.last_run_at is not None and got.next_run_at is not None
    assert schedules_repo.list_due(conn, now=NOW) == []  # advanced past now → not due


def test_set_enabled(conn) -> None:
    s = schedules_repo.create_schedule(conn, kind="a", schedule_cron="@daily")
    schedules_repo.set_enabled(conn, s.id, False)
    got = schedules_repo.get_schedule(conn, s.id)
    assert got is not None and got.enabled is False
    assert len(schedules_repo.list_schedules(conn, enabled_only=True)) == 0
