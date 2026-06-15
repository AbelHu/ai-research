"""Tests for final-report assembly + Librarian commit (implementation-plan T5.8)."""

from __future__ import annotations

import json

import pytest

from app.memory.library import get_index_entry, library_root
from app.memory.reports import (
    FinalReport,
    Gain,
    ImprovementSuggestion,
    commit_final_report,
)
from app.storage.db import connect
from app.storage.migrations import migrate
from app.storage.repos import library as library_repo
from app.storage.repos import requests as requests_repo


@pytest.fixture
def db():
    conn = connect()
    migrate(conn)
    try:
        yield conn
    finally:
        conn.close()


def _seed_request(conn, *, kind="ask", title="capital of France"):
    req = requests_repo.create_request(conn, title=title)
    requests_repo.create_job(conn, request_id=req.id, kind=kind, complexity="simple")
    return req


def _report(req, **over) -> FinalReport:
    base = dict(
        request_id=req.id,
        kind="ask",
        title="What is the capital of France?",
        keywords=["paris", "capital", "france"],
        tags=["geo"],
        brief_description="Asked for and delivered the capital of France: Paris.",
        outcome="delivered",
        gain=Gain(good="memory hit answered it", improve="cache common geography"),
        improvement_suggestions=[
            ImprovementSuggestion(title="extract a geography skill", effort="low")
        ],
    )
    base.update(over)
    return FinalReport(**base)


def test_commit_writes_db_and_disk(db, tmp_path) -> None:
    req = _seed_request(db)
    root = library_root(tmp_path)
    result = commit_final_report(db, root, _report(req))

    # DB: final_reports card + library_index mirror.
    fr = library_repo.get_final_report(db, result.final_report_id)
    assert fr["request_id"] == req.id
    assert json.loads(fr["keywords_json"]) == ["paris", "capital", "france"]
    assert fr["gain_good"] == "memory hit answered it"
    li = library_repo.get_library_index_for_request(db, req.id)
    assert li is not None
    assert li["folder_path"] == str(result.folder)

    # Disk: final_report.md + final_report.json under Active/Simple/<code>/.
    assert result.report_path.is_file()
    assert "Paris" in result.report_path.read_text(encoding="utf-8")
    rendered = json.loads((result.folder / "final_report.json").read_text(encoding="utf-8"))
    assert rendered["title"] == "What is the capital of France?"
    assert result.folder.parent.name == "Simple"

    # Disk index.json carries the entry keyed by the request code.
    entry = get_index_entry(root, req.code)
    assert entry is not None
    assert entry["tags"] == ["geo"]


def test_db_refs_link_card_and_job(db, tmp_path) -> None:
    req = _seed_request(db, kind="task")
    root = library_root(tmp_path)
    result = commit_final_report(db, root, _report(req, kind="task"))
    li = library_repo.get_library_index_for_request(db, req.id)
    refs = json.loads(li["db_refs_json"])
    assert refs["final_report_id"] == result.final_report_id
    assert refs["job_id"] is not None
    assert result.folder.parent.name == "Tasks"


def test_unknown_request_rejected(db, tmp_path) -> None:
    bogus = FinalReport(request_id=9999, kind="ask", title="x")
    with pytest.raises(ValueError):
        commit_final_report(db, library_root(tmp_path), bogus)


def test_minimal_report_commits(db, tmp_path) -> None:
    # A minimal valid report (only required fields) still commits.
    req = _seed_request(db)
    minimal = FinalReport(request_id=req.id, kind="ask", title="minimal")
    result = commit_final_report(db, library_root(tmp_path), minimal)
    assert library_repo.get_final_report(db, result.final_report_id) is not None


def test_report_rejects_unknown_field() -> None:
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        FinalReport(request_id=1, kind="ask", title="x", made_up="nope")
