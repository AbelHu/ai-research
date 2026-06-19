"""Tests for the Librarian's cold-library compaction (design-spec §9.2)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.config.policies import MemoryPolicy
from app.memory import archive
from app.roles import librarian
from app.storage.db import connect
from app.storage.migrations import migrate
from app.storage.repos import library as library_repo
from app.storage.repos import requests as requests_repo

_KEEP = frozenset({"final_report.md", "final_report.json"})


@pytest.fixture
def conn():
    c = connect()
    migrate(c)
    try:
        yield c
    finally:
        c.close()


def _utc_naive() -> datetime:
    return datetime.now(tz=timezone.utc).replace(tzinfo=None)


def _closed_request(conn, tmp_path, *, with_report: bool = True):
    """A committed request: a library folder + a mirror row pointing at it."""
    req = requests_repo.create_request(conn, title="some finished work")
    folder = tmp_path / "library" / "Active" / "Simple" / req.code
    folder.mkdir(parents=True)
    if with_report:
        (folder / "final_report.md").write_text("# Report\n\nthe summary", encoding="utf-8")
        (folder / "final_report.json").write_text('{"k": 1}', encoding="utf-8")
    # Bulky artifacts that should be zipped away.
    (folder / "process.log").write_text("trace line\n" * 500, encoding="utf-8")
    (folder / "phase1").mkdir()
    (folder / "phase1" / "notes.txt").write_text("working notes", encoding="utf-8")
    library_repo.create_library_index_entry(conn, request_id=req.id, folder_path=str(folder))
    return req, folder


def test_compacts_a_cold_folder_keeping_the_report(conn, tmp_path) -> None:
    req, folder = _closed_request(conn, tmp_path)
    later = _utc_naive() + timedelta(days=40)  # past the 30-day default

    result = librarian.compact_cold_library(conn, now=later)

    assert folder.name in result.compacted
    assert result.bytes_saved > 0
    assert (folder / "artifacts.zip").is_file()
    # The final report stays readable; the bulky artifacts are zipped away.
    assert (folder / "final_report.md").is_file()
    assert (folder / "final_report.json").is_file()
    assert not (folder / "process.log").exists()
    assert not (folder / "phase1").exists()  # empty dir removed too


def test_skips_recently_accessed(conn, tmp_path) -> None:
    req, folder = _closed_request(conn, tmp_path)
    base = _utc_naive()
    library_repo.touch_library_index(conn, req.id, now=base)  # accessed "now"

    # Five days later — still inside the 30-day window → left hot.
    result = librarian.compact_cold_library(conn, now=base + timedelta(days=5))
    assert result.compacted == []
    assert not (folder / "artifacts.zip").exists()


def test_skips_already_compacted(conn, tmp_path) -> None:
    req, folder = _closed_request(conn, tmp_path)
    archive.compact_folder(folder, keep=_KEEP)  # already cold

    result = librarian.compact_cold_library(conn, now=_utc_naive() + timedelta(days=40))
    assert result.compacted == []


def test_skips_folder_without_final_report(conn, tmp_path) -> None:
    req, folder = _closed_request(conn, tmp_path, with_report=False)
    result = librarian.compact_cold_library(conn, now=_utc_naive() + timedelta(days=40))
    assert result.compacted == []
    assert not (folder / "artifacts.zip").exists()


def test_disabled_when_after_days_is_zero(conn, tmp_path) -> None:
    req, folder = _closed_request(conn, tmp_path)
    pol = MemoryPolicy(compact_library_after_days=0)
    result = librarian.compact_cold_library(conn, now=_utc_naive() + timedelta(days=40), policy=pol)
    assert result.compacted == []
    assert not (folder / "artifacts.zip").exists()


def test_revive_restores_artifacts_and_marks_accessed(conn, tmp_path) -> None:
    req, folder = _closed_request(conn, tmp_path)
    librarian.compact_cold_library(conn, now=_utc_naive() + timedelta(days=40))
    assert (folder / "artifacts.zip").is_file()

    restored = librarian.revive_library_folder(conn, req.id)

    assert "process.log" in restored
    assert (folder / "process.log").is_file()
    assert not (folder / "artifacts.zip").exists()  # back to hot
    row = library_repo.get_library_index_for_request(conn, req.id)
    assert row["last_used_at"] is not None  # access recorded → won't re-compact soon


def test_touch_index_throttles_writes_within_the_window(conn, tmp_path) -> None:
    req, _ = _closed_request(conn, tmp_path)
    base = _utc_naive()

    # No refresh → always writes (the explicit-revive path).
    assert library_repo.touch_library_index(conn, req.id, now=base) is True
    # Within the refresh window → skipped, timestamp untouched.
    assert (
        library_repo.touch_library_index(
            conn, req.id, now=base + timedelta(minutes=30), refresh=timedelta(hours=1)
        )
        is False
    )
    before = library_repo.get_library_index_for_request(conn, req.id)["last_used_at"]
    # Past the window → writes.
    assert (
        library_repo.touch_library_index(
            conn, req.id, now=base + timedelta(hours=2), refresh=timedelta(hours=1)
        )
        is True
    )
    after = library_repo.get_library_index_for_request(conn, req.id)["last_used_at"]
    assert after != before


def test_note_library_access_throttled_by_policy(conn, tmp_path) -> None:
    req, _ = _closed_request(conn, tmp_path)
    base = _utc_naive()
    pol = MemoryPolicy(library_access_refresh_hours=24)

    assert librarian.note_library_access(conn, req.id, now=base, policy=pol) is True
    # 1h later — inside the 24h window → throttled (no write).
    assert (
        librarian.note_library_access(conn, req.id, now=base + timedelta(hours=1), policy=pol)
        is False
    )
    # 25h later — past the window → refreshes.
    assert (
        librarian.note_library_access(conn, req.id, now=base + timedelta(hours=25), policy=pol)
        is True
    )
    # hours=0 disables the throttle → every read refreshes.
    pol0 = MemoryPolicy(library_access_refresh_hours=0)
    assert (
        librarian.note_library_access(conn, req.id, now=base + timedelta(hours=25), policy=pol0)
        is True
    )


def test_read_library_report_returns_text_and_records_access(conn, tmp_path) -> None:
    req, _ = _closed_request(conn, tmp_path)
    text = librarian.read_library_report(conn, req.id, now=_utc_naive())
    assert text is not None and "Report" in text
    row = library_repo.get_library_index_for_request(conn, req.id)
    assert row["last_used_at"] is not None  # the read kept the folder hot


def test_read_library_report_works_on_a_compacted_folder(conn, tmp_path) -> None:
    req, folder = _closed_request(conn, tmp_path)
    librarian.compact_cold_library(conn, now=_utc_naive() + timedelta(days=40))
    assert (folder / "artifacts.zip").is_file()

    # The report stays uncompressed, so reading needs no revive.
    text = librarian.read_library_report(conn, req.id, now=_utc_naive())
    assert text is not None and "Report" in text
    assert (folder / "artifacts.zip").is_file()  # read != revive — still compacted


def test_read_library_report_is_none_without_a_report(conn, tmp_path) -> None:
    req, _ = _closed_request(conn, tmp_path, with_report=False)
    assert librarian.read_library_report(conn, req.id) is None
