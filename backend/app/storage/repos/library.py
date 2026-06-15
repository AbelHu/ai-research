"""Final-report + library-index repository (design-spec §9.2; plan T5.8).

Typed writes/reads for the two durable library tables: ``final_reports`` (one
per job, the card the user confirms) and ``library_index`` (the DB mirror of the
on-disk ``index.json``, holding active/archived entries only). JSON columns are
stored as compact JSON text.
"""

from __future__ import annotations

import json
import sqlite3


def create_final_report(
    conn: sqlite3.Connection,
    *,
    request_id: int,
    job_id: int | None = None,
    keywords: list[str] | None = None,
    tags: list[str] | None = None,
    brief_description: str | None = None,
    gain_good: str | None = None,
    gain_bad: str | None = None,
    gain_improve: str | None = None,
    improvement_suggestions: list[dict] | None = None,
    outcome: str | None = None,
    artifact_path: str | None = None,
) -> int:
    """Insert a final-report row; return its id."""
    with conn:
        cur = conn.execute(
            "INSERT INTO final_reports "
            "(request_id, job_id, keywords_json, tags_json, brief_description, "
            " gain_good, gain_bad, gain_improve, improvement_suggestions_json, "
            " outcome, artifact_path) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                request_id,
                job_id,
                json.dumps(keywords or []),
                json.dumps(tags or []),
                brief_description,
                gain_good,
                gain_bad,
                gain_improve,
                json.dumps(improvement_suggestions or []),
                outcome,
                artifact_path,
            ),
        )
    return int(cur.lastrowid)


def get_final_report(conn: sqlite3.Connection, final_report_id: int) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM final_reports WHERE id = ?", (final_report_id,)).fetchone()


def get_final_report_for_request(conn: sqlite3.Connection, request_id: int) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM final_reports WHERE request_id = ? ORDER BY id DESC LIMIT 1",
        (request_id,),
    ).fetchone()


def set_final_report_confirmation(
    conn: sqlite3.Connection,
    final_report_id: int,
    *,
    user_confirmed: bool,
    spawned_request_id: int | None = None,
) -> None:
    """Record the user's confirmation + any spawned improvement request (§6B)."""
    with conn:
        conn.execute(
            "UPDATE final_reports SET user_confirmed = ?, spawned_request_id = ? WHERE id = ?",
            (1 if user_confirmed else 0, spawned_request_id, final_report_id),
        )


def create_library_index_entry(
    conn: sqlite3.Connection,
    *,
    request_id: int,
    object_type: str = "request",
    keywords: list[str] | None = None,
    tags: list[str] | None = None,
    brief_description: str | None = None,
    folder_path: str | None = None,
    db_refs: dict | None = None,
) -> int:
    """Insert a library-index mirror row; return its id."""
    with conn:
        cur = conn.execute(
            "INSERT INTO library_index "
            "(request_id, object_type, keywords_json, tags_json, brief_description, "
            " folder_path, db_refs_json) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                request_id,
                object_type,
                json.dumps(keywords or []),
                json.dumps(tags or []),
                brief_description,
                folder_path,
                json.dumps(db_refs or {}),
            ),
        )
    return int(cur.lastrowid)


def get_library_index_for_request(conn: sqlite3.Connection, request_id: int) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM library_index WHERE request_id = ? ORDER BY id DESC LIMIT 1",
        (request_id,),
    ).fetchone()


def delete_library_index_for_request(conn: sqlite3.Connection, request_id: int) -> int:
    """Delete a request's library-index mirror rows (the drop path, §9.1).

    Returns the number of rows removed. The `final_reports` card is **kept** —
    only the hot mirror row goes (the on-disk entry moves to index.dropped).
    """
    with conn:
        cur = conn.execute("DELETE FROM library_index WHERE request_id = ?", (request_id,))
    return cur.rowcount
