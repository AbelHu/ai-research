"""Tests for the scheduler worker (design-spec §9, §11)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.cli import schedworker
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


def test_ensure_default_seeds_memory_maintenance_idempotently(conn) -> None:
    schedworker.ensure_default_schedules(conn)
    s = schedules_repo.get_by_kind(conn, schedworker.MEMORY_MAINTENANCE)
    assert s is not None and s.schedule_cron == "@daily"

    schedworker.ensure_default_schedules(conn)  # idempotent — no duplicate
    same_kind = [
        x for x in schedules_repo.list_schedules(conn) if x.kind == schedworker.MEMORY_MAINTENANCE
    ]
    assert len(same_kind) == 1


def test_ensure_default_seeds_library_compaction(conn) -> None:
    schedworker.ensure_default_schedules(conn)
    s = schedules_repo.get_by_kind(conn, schedworker.LIBRARY_COMPACTION)
    assert s is not None and s.schedule_cron == "@daily"
    assert schedworker.LIBRARY_COMPACTION in schedworker.HANDLERS


def test_run_due_runs_handler_and_advances(conn, monkeypatch) -> None:
    ran: list[str] = []
    monkeypatch.setitem(schedworker.HANDLERS, "testkind", lambda _c: ran.append("x") or "ok")
    schedules_repo.create_schedule(
        conn, kind="testkind", schedule_cron="@daily", next_run_at=NOW - timedelta(hours=1)
    )

    count = schedworker.run_due_schedules(conn, now=NOW)

    assert count == 1 and ran == ["x"]
    # Advanced ~1 day → no longer due at NOW.
    assert schedules_repo.list_due(conn, now=NOW) == []


def test_run_due_unknown_kind_is_skipped_but_advanced(conn) -> None:
    schedules_repo.create_schedule(
        conn, kind="no_handler", schedule_cron="@hourly", next_run_at=NOW - timedelta(hours=1)
    )
    schedworker.run_due_schedules(conn, now=NOW)
    # Advanced so the unhandled row doesn't re-fire every poll.
    assert schedules_repo.list_due(conn, now=NOW) == []


def test_failing_handler_does_not_stop_other_schedules(conn, monkeypatch) -> None:
    ran: list[str] = []

    def _boom(_c):
        raise RuntimeError("boom")

    monkeypatch.setitem(schedworker.HANDLERS, "bad", _boom)
    monkeypatch.setitem(schedworker.HANDLERS, "good", lambda _c: ran.append("g") or "ok")
    past = NOW - timedelta(hours=1)
    schedules_repo.create_schedule(conn, kind="bad", schedule_cron="@daily", next_run_at=past)
    schedules_repo.create_schedule(conn, kind="good", schedule_cron="@daily", next_run_at=past)

    schedworker.run_due_schedules(conn, now=NOW)

    assert ran == ["g"]  # the good schedule still ran despite the bad one failing


def test_serve_once_seeds_and_runs_memory_maintenance(conn) -> None:
    rc = schedworker.serve_schedules(conn, once=True)
    assert rc == 0
    s = schedules_repo.get_by_kind(conn, schedworker.MEMORY_MAINTENANCE)
    assert s is not None and s.last_run_at is not None  # it ran on the first pass
