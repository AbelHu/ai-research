"""Tests for the daily API-usage counter (web-search budget cap)."""

from __future__ import annotations

import pytest

from app.storage.db import connect
from app.storage.migrations import migrate
from app.storage.repos import api_usage as repo


@pytest.fixture
def conn():
    c = connect()
    migrate(c)
    try:
        yield c
    finally:
        c.close()


def test_count_starts_at_zero(conn) -> None:
    assert repo.count_today(conn, "tavily") == 0


def test_increment_accumulates_per_day(conn) -> None:
    assert repo.increment(conn, "tavily", day="2026-06-16") == 1
    assert repo.increment(conn, "tavily", day="2026-06-16") == 2
    assert repo.increment(conn, "tavily", day="2026-06-16", amount=3) == 5
    assert repo.count_today(conn, "tavily", day="2026-06-16") == 5
    # A different day is a separate counter (the cap resets daily).
    assert repo.count_today(conn, "tavily", day="2026-06-17") == 0


def test_providers_are_independent(conn) -> None:
    repo.increment(conn, "tavily", day="2026-06-16")
    assert repo.count_today(conn, "brave", day="2026-06-16") == 0
