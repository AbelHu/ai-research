"""The Librarian — deterministic library + memory upkeep (design-spec §9, §9.1).

The Librarian is the company's archival role and the **sole writer** of the
archive and memory tables. Its standing, scheduled duty exposed here is the
**daily memory-maintenance sweep**: expire/drop, archive, promote, and
consolidate the active memory set per the TTL/weight policy
(`app.memory.sweep`). It runs **no model** — the sweep is fully deterministic.
The scheduler (`app.cli.schedworker`) invokes this on the configured interval.
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app.config.policies import MemoryPolicy, get_policies
from app.memory import archive
from app.memory.sweep import SweepResult, sweep
from app.storage.repos import library as library_repo

logger = logging.getLogger("app.roles.librarian")

# The final-report files kept uncompressed so a cold folder stays searchable.
_KEEP_FILES = frozenset({"final_report.md", "final_report.json"})


def run_memory_maintenance(
    conn: sqlite3.Connection,
    *,
    now: datetime | None = None,
    policy: MemoryPolicy | None = None,
) -> SweepResult:
    """Run the daily memory sweep and log what changed (§9.1).

    Drops expired low-value memories, archives stale ones, promotes well-used
    short-term memories to long-term, and consolidates duplicates — never
    touching ``core`` memories. Returns the per-phase `SweepResult`.
    """
    result = sweep(conn, now=now, policy=policy)
    logger.info(
        "librarian memory maintenance: dropped=%d archived=%d promoted=%d consolidated=%d",
        len(result.dropped),
        len(result.archived),
        len(result.promoted),
        len(result.consolidated),
    )
    return result


def _parse_ts(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _folder_size(folder: Path) -> int:
    return sum(p.stat().st_size for p in folder.rglob("*") if p.is_file())


@dataclass(frozen=True)
class LibraryCompactionResult:
    """What a cold-library compaction pass changed."""

    compacted: list[str] = field(default_factory=list)  # request codes (folder names)
    bytes_saved: int = 0


def compact_cold_library(
    conn: sqlite3.Connection,
    *,
    now: datetime | None = None,
    policy: MemoryPolicy | None = None,
) -> LibraryCompactionResult:
    """Zip the artifacts of closed-request folders gone quiet (design-spec §9.2).

    A committed request's folder is compacted (everything **except** the final
    report is zipped into ``artifacts.zip``) once it has not been accessed for
    ``compact_library_after_days``. Already-compacted folders and recently-used
    ones are skipped; the final report stays readable for search/preview, and a
    later access can revive the folder (`revive_library_folder`). Returns the
    request codes compacted + the bytes reclaimed.
    """
    pol = policy if policy is not None else get_policies().memory
    after_days = pol.compact_library_after_days
    if after_days <= 0:  # disabled
        return LibraryCompactionResult()
    moment = now if now is not None else datetime.now(tz=timezone.utc).replace(tzinfo=None)
    threshold = timedelta(days=after_days)

    compacted: list[str] = []
    saved = 0
    for row in library_repo.list_library_index(conn):
        folder_path = row["folder_path"]
        if not folder_path:
            continue
        folder = Path(folder_path)
        # Only compact a committed (closed) request that isn't already cold.
        if not folder.is_dir() or not (folder / "final_report.md").is_file():
            continue
        if archive.is_compacted(folder):
            continue
        last = _parse_ts(row["last_used_at"]) or _parse_ts(row["created_at"])
        if last is None or (moment - last) < threshold:
            continue  # still recent (or unknown age) — leave it hot
        before = _folder_size(folder)
        archive.compact_folder(folder, keep=_KEEP_FILES)
        saved += max(0, before - _folder_size(folder))
        compacted.append(folder.name)

    if compacted:
        logger.info(
            "librarian library compaction: compacted %d folder(s), saved %d bytes (%s)",
            len(compacted),
            saved,
            ", ".join(compacted),
        )
    return LibraryCompactionResult(compacted=compacted, bytes_saved=saved)


def revive_library_folder(conn: sqlite3.Connection, request_id: int) -> list[str]:
    """Cold→hot read for a library folder: unzip its artifacts + mark accessed (§9.1).

    Restores a compacted request's files (a no-op if it wasn't compacted) and
    stamps ``last_used_at`` so the next compaction pass leaves it alone. Returns
    the restored file names. This is the access path that satisfies "don't
    compact what's being accessed via memory".
    """
    row = library_repo.get_library_index_for_request(conn, request_id)
    library_repo.touch_library_index(conn, request_id)
    if row is None or not row["folder_path"]:
        return []
    return archive.revive_folder(Path(row["folder_path"]))


def note_library_access(
    conn: sqlite3.Connection,
    request_id: int,
    *,
    now: datetime | None = None,
    policy: MemoryPolicy | None = None,
) -> bool:
    """Record that a committed library folder was read — a throttled touch (§9.2).

    The relatime hook for the *hot* read path: stamps ``last_used_at`` so an
    actively-read folder stays out of `compact_cold_library`, but at most once
    per ``library_access_refresh_hours`` so a read-heavy burst doesn't amplify
    into a write per read. Returns whether the access clock was refreshed.
    """
    pol = policy if policy is not None else get_policies().memory
    refresh = timedelta(hours=pol.library_access_refresh_hours)
    return library_repo.touch_library_index(conn, request_id, now=now, refresh=refresh)


def read_library_report(
    conn: sqlite3.Connection,
    request_id: int,
    *,
    now: datetime | None = None,
    policy: MemoryPolicy | None = None,
) -> str | None:
    """Read a committed request's final report, recording the access (§9.2).

    The folder-read path: returns the ``final_report.md`` text — which stays
    uncompressed even on a compacted folder, so no revive is needed just to read
    it — and records a throttled access (`note_library_access`) so a frequently
    read folder is kept hot. Returns ``None`` when the request has no library
    folder/report. For the full artifact set use `revive_library_folder`.
    """
    row = library_repo.get_library_index_for_request(conn, request_id)
    if row is None or not row["folder_path"]:
        return None
    report = Path(row["folder_path"]) / "final_report.md"
    if not report.is_file():
        return None
    text = report.read_text(encoding="utf-8")
    note_library_access(conn, request_id, now=now, policy=policy)
    return text
