"""Tests for interval scheduling helpers (design-spec §9, §11)."""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from app.scheduling import DEFAULT_INTERVAL, next_run_after, parse_interval


@pytest.mark.parametrize(
    "spec,seconds",
    [
        ("@hourly", 3600),
        ("@daily", 86400),
        ("@weekly", 604800),
        ("@DAILY", 86400),  # case-insensitive
        ("@every 6h", 6 * 3600),
        ("@every 30m", 30 * 60),
        ("@every 90s", 90),
        ("@every 2d", 2 * 86400),
        ("6h", 6 * 3600),  # bare unit
        ("3600", 3600),  # bare seconds
    ],
)
def test_parse_interval_recognized(spec, seconds) -> None:
    assert parse_interval(spec) == timedelta(seconds=seconds)


@pytest.mark.parametrize("spec", [None, "", "   ", "garbage", "@yearly", "5x", "@every", "h"])
def test_parse_interval_unrecognized(spec) -> None:
    assert parse_interval(spec) is None


def test_next_run_after_uses_interval() -> None:
    base = datetime(2026, 1, 1, 0, 0)
    assert next_run_after("@daily", base) == base + timedelta(days=1)
    assert next_run_after("@every 6h", base) == base + timedelta(hours=6)


def test_next_run_after_falls_back_to_default() -> None:
    base = datetime(2026, 1, 1, 0, 0)
    assert next_run_after("garbage", base) == base + DEFAULT_INTERVAL
    assert next_run_after(None, base) == base + DEFAULT_INTERVAL
