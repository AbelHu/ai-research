"""Tests for the on-disk folder library + index files (implementation-plan T5.7)."""

from __future__ import annotations

import json

import pytest

from app.memory.library import (
    IndexEntry,
    active_dir,
    ensure_request_folder,
    get_index_entry,
    library_root,
    load_index,
    move_to_dropped,
    request_folder,
    upsert_index_entry,
)


def test_kind_maps_to_subdir(tmp_path) -> None:
    root = library_root(tmp_path)
    assert active_dir(root, "ask").name == "Simple"
    assert active_dir(root, "task").name == "Tasks"
    assert active_dir(root, "feature").name == "Features"


def test_unknown_kind_rejected(tmp_path) -> None:
    with pytest.raises(ValueError):
        active_dir(library_root(tmp_path), "bogus")


def test_ensure_request_folder_creates_it(tmp_path) -> None:
    root = library_root(tmp_path)
    folder = ensure_request_folder(root, "ask", "20260615120000")
    assert folder.is_dir()
    assert folder == request_folder(root, "ask", "20260615120000")
    assert folder.parent.name == "Simple"


def test_upsert_writes_index_json(tmp_path) -> None:
    root = library_root(tmp_path)
    folder = ensure_request_folder(root, "ask", "20260615120000")
    entry = IndexEntry(
        request_code="20260615120000",
        folder_path=str(folder),
        keywords=["paris", "capital"],
        tags=["geo"],
        brief_description="capital of France",
    )
    upsert_index_entry(root, entry)

    on_disk = json.loads((root / "index.json").read_text(encoding="utf-8"))
    assert "20260615120000" in on_disk
    assert on_disk["20260615120000"]["keywords"] == ["paris", "capital"]
    assert get_index_entry(root, "20260615120000")["brief_description"] == "capital of France"


def test_upsert_updates_existing(tmp_path) -> None:
    root = library_root(tmp_path)
    code = "20260615120001"
    upsert_index_entry(root, IndexEntry(request_code=code, folder_path="a", tags=["old"]))
    upsert_index_entry(root, IndexEntry(request_code=code, folder_path="a", tags=["new"]))
    index = load_index(root)
    assert len(index) == 1
    assert index[code]["tags"] == ["new"]


def test_move_to_dropped_relocates_entry(tmp_path) -> None:
    root = library_root(tmp_path)
    code = "20260615120002"
    upsert_index_entry(root, IndexEntry(request_code=code, folder_path="f"))

    assert move_to_dropped(root, code) is True
    assert get_index_entry(root, code) is None  # gone from index.json
    assert get_index_entry(root, code, dropped=True) is not None  # in index.dropped.json
    # Idempotent: a second move finds nothing.
    assert move_to_dropped(root, code) is False


def test_load_index_missing_file_is_empty(tmp_path) -> None:
    assert load_index(library_root(tmp_path)) == {}
