"""Tests for archive compaction + revive (implementation-plan T5.9)."""

from __future__ import annotations

import zipfile

import pytest

from app.memory.archive import (
    ARCHIVE_NAME,
    compact_folder,
    is_compacted,
    revive_folder,
)


@pytest.fixture
def folder(tmp_path):
    f = tmp_path / "20260615120000"
    (f / "phases" / "1").mkdir(parents=True)
    (f / "final_report.md").write_text("# Report\nParis.\n", encoding="utf-8")
    (f / "process.log").write_text("ran memory.search\n", encoding="utf-8")
    (f / "artifacts").mkdir()
    (f / "artifacts" / "out.txt").write_text("deliverable\n", encoding="utf-8")
    (f / "phases" / "1" / "phase_report.md").write_text("phase 1\n", encoding="utf-8")
    return f


def test_compact_zips_all_but_final_report(folder) -> None:
    zip_path = compact_folder(folder)

    assert zip_path.name == ARCHIVE_NAME
    assert is_compacted(folder)
    # final_report.md stays readable, uncompressed.
    assert (folder / "final_report.md").read_text(encoding="utf-8").startswith("# Report")
    # The other files are gone from disk (now inside the zip).
    assert not (folder / "process.log").exists()
    assert not (folder / "artifacts" / "out.txt").exists()

    with zipfile.ZipFile(zip_path) as zf:
        names = set(zf.namelist())
    assert "process.log" in names
    assert "artifacts/out.txt" in names
    assert "phases/1/phase_report.md" in names
    assert "final_report.md" not in names  # kept out of the archive


def test_revive_restores_everything(folder) -> None:
    compact_folder(folder)
    restored = revive_folder(folder)

    assert not is_compacted(folder)  # zip removed
    assert set(restored) == {"process.log", "artifacts/out.txt", "phases/1/phase_report.md"}
    assert (folder / "process.log").read_text(encoding="utf-8") == "ran memory.search\n"
    assert (folder / "artifacts" / "out.txt").read_text(encoding="utf-8") == "deliverable\n"
    assert (folder / "phases" / "1" / "phase_report.md").is_file()


def test_round_trip_is_lossless(folder) -> None:
    before = {
        p.relative_to(folder).as_posix(): p.read_text(encoding="utf-8")
        for p in folder.rglob("*")
        if p.is_file()
    }
    compact_folder(folder)
    revive_folder(folder)
    after = {
        p.relative_to(folder).as_posix(): p.read_text(encoding="utf-8")
        for p in folder.rglob("*")
        if p.is_file()
    }
    assert after == before


def test_revive_without_archive_is_noop(tmp_path) -> None:
    f = tmp_path / "empty"
    f.mkdir()
    assert revive_folder(f) == []
