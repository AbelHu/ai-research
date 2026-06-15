"""Final report schema + Librarian commit (design-spec §9.2; plan T5.8).

The §9.2 final report turns one-off work into reusable memory. It is assembled
(by the Junior Worker for asks, the Plan Expert for jobs), **validated** into the
`FinalReport` pydantic model, then **committed by the Librarian** — the single
writer — to three places at once: the `final_reports` card, the `library_index`
hot mirror, and the on-disk request folder (`final_report.md` + `.json` +
`index.json`).
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.memory.library import IndexEntry, ensure_request_folder, upsert_index_entry
from app.storage.repos import library as library_repo
from app.storage.repos import requests as requests_repo

Kind = Literal["ask", "task", "feature"]
Outcome = Literal["delivered", "partial", "abandoned"]


class Gain(BaseModel):
    """The 'experience' captured by a finished request (§9.2)."""

    model_config = ConfigDict(extra="forbid")
    good: str = ""
    bad: str = ""
    improve: str = ""


class ImprovementSuggestion(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: str
    benefit: str = ""
    effort: Literal["low", "med", "high"] = "med"


class Promotions(BaseModel):
    model_config = ConfigDict(extra="forbid")
    skills: list[str] = Field(default_factory=list)
    memories: list[str] = Field(default_factory=list)
    interests: list[str] = Field(default_factory=list)


class FinalReport(BaseModel):
    """The validated §9.2 final-report contract (one per request)."""

    model_config = ConfigDict(extra="forbid")

    request_id: int
    kind: Kind
    title: str
    keywords: list[str] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)
    brief_description: str = ""
    outcome: Outcome = "delivered"
    result_ref: str | None = None
    gain: Gain = Field(default_factory=Gain)
    improvement_suggestions: list[ImprovementSuggestion] = Field(default_factory=list)
    promotions: Promotions = Field(default_factory=Promotions)


@dataclass(frozen=True)
class CommitResult:
    """What the Librarian wrote when committing a final report."""

    final_report_id: int
    library_index_id: int
    folder: Path
    report_path: Path


def render_markdown(report: FinalReport, request_code: str) -> str:
    """Render the human-readable ``final_report.md`` body."""
    lines = [
        f"# {report.title}",
        "",
        f"- **request:** /req {request_code}",
        f"- **kind:** {report.kind}",
        f"- **outcome:** {report.outcome}",
        f"- **keywords:** {', '.join(report.keywords) or '—'}",
        f"- **tags:** {', '.join(report.tags) or '—'}",
        "",
        "## Summary",
        report.brief_description or "—",
        "",
        "## Gain",
        f"- **Good:** {report.gain.good or '—'}",
        f"- **Bad:** {report.gain.bad or '—'}",
        f"- **Improve:** {report.gain.improve or '—'}",
    ]
    if report.improvement_suggestions:
        lines += ["", "## Improvement suggestions"]
        lines += [
            f"- {s.title} (benefit: {s.benefit or '—'}, effort: {s.effort})"
            for s in report.improvement_suggestions
        ]
    return "\n".join(lines) + "\n"


def commit_final_report(
    conn: sqlite3.Connection,
    library_root: Path,
    report: FinalReport,
) -> CommitResult:
    """Librarian commit: write the card + mirror + folder for a final report.

    Single-writer: this is the only path that writes `final_reports`,
    `library_index`, and the request's library folder for a finished request.
    """
    request = requests_repo.get_request(conn, report.request_id)
    if request is None:
        raise ValueError(f"unknown request_id: {report.request_id}")
    job = requests_repo.get_job_for_request(conn, report.request_id)
    job_id = job.id if job is not None else None

    # 1) On-disk folder: final_report.md + final_report.json.
    folder = ensure_request_folder(library_root, report.kind, request.code)
    report_path = folder / "final_report.md"
    report_path.write_text(render_markdown(report, request.code), encoding="utf-8")
    (folder / "final_report.json").write_text(
        json.dumps(report.model_dump(), indent=2, ensure_ascii=False), encoding="utf-8"
    )

    # 2) Durable card.
    final_report_id = library_repo.create_final_report(
        conn,
        request_id=report.request_id,
        job_id=job_id,
        keywords=report.keywords,
        tags=report.tags,
        brief_description=report.brief_description,
        gain_good=report.gain.good,
        gain_bad=report.gain.bad,
        gain_improve=report.gain.improve,
        improvement_suggestions=[s.model_dump() for s in report.improvement_suggestions],
        outcome=report.outcome,
        artifact_path=str(folder),
    )

    # 3) Hot DB mirror + on-disk index.json entry.
    library_index_id = library_repo.create_library_index_entry(
        conn,
        request_id=report.request_id,
        object_type="request",
        keywords=report.keywords,
        tags=report.tags,
        brief_description=report.brief_description,
        folder_path=str(folder),
        db_refs={"final_report_id": final_report_id, "job_id": job_id},
    )
    upsert_index_entry(
        library_root,
        IndexEntry(
            request_code=request.code,
            folder_path=str(folder),
            keywords=report.keywords,
            tags=report.tags,
            brief_description=report.brief_description,
        ),
    )

    return CommitResult(
        final_report_id=final_report_id,
        library_index_id=library_index_id,
        folder=folder,
        report_path=report_path,
    )
