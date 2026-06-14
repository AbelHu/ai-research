"""Schema round-trip tests for migrations 0001-0006 (implementation-plan T1.3-T1.8).

Each test migrates a fresh in-memory database and inserts/selects through one
migration's tables, exercising foreign keys, enum CHECKs, and recursive/self
references. Pure storage - no AI.
"""

from __future__ import annotations

import sqlite3

import pytest

from app.storage.db import connect
from app.storage.migrations import migrate


@pytest.fixture
def conn():
    c = connect()
    migrate(c)
    try:
        yield c
    finally:
        c.close()


def _new_user(conn: sqlite3.Connection, *, owner: bool = True) -> int:
    cur = conn.execute(
        "INSERT INTO users (display_name, github_login, is_owner) VALUES (?, ?, ?)",
        ("Owner", "owner-gh", 1 if owner else 0),
    )
    return int(cur.lastrowid)


def _new_request(conn: sqlite3.Connection, code: str, user_id: int) -> int:
    cur = conn.execute(
        "INSERT INTO requests (code, user_id, title, status) VALUES (?, ?, ?, ?)",
        (code, user_id, "a title", "open"),
    )
    return int(cur.lastrowid)


def _new_job(conn: sqlite3.Connection, request_id: int, kind: str = "task") -> int:
    cur = conn.execute(
        "INSERT INTO jobs (request_id, kind) VALUES (?, ?)",
        (request_id, kind),
    )
    return int(cur.lastrowid)


# --- T1.3 identity -----------------------------------------------------------
def test_identity_tables_round_trip(conn: sqlite3.Connection) -> None:
    user_id = _new_user(conn)
    conn.execute(
        "INSERT INTO user_identities (user_id, channel, channel_user_id, state) "
        "VALUES (?, ?, ?, 'paired')",
        (user_id, "telegram", "12345"),
    )
    conn.execute(
        "INSERT INTO user_traits (user_id, key, value, confidence) VALUES (?, ?, ?, ?)",
        (user_id, "location:home", "Paris", 0.9),
    )
    sess = conn.execute(
        "INSERT INTO sessions (user_id, channel, status) VALUES (?, 'telegram', 'open')",
        (user_id,),
    ).lastrowid
    conn.execute(
        "INSERT INTO messages (session_id, direction, content) VALUES (?, 'in', 'hi')",
        (sess,),
    )
    conn.commit()

    owner = conn.execute("SELECT is_owner FROM users WHERE id = ?", (user_id,)).fetchone()
    assert owner["is_owner"] == 1
    trait = conn.execute(
        "SELECT value FROM user_traits WHERE user_id = ? AND key = 'location:home'",
        (user_id,),
    ).fetchone()
    assert trait["value"] == "Paris"


def test_identity_rejects_bad_identity_state(conn: sqlite3.Connection) -> None:
    user_id = _new_user(conn)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO user_identities (user_id, channel, channel_user_id, state) "
            "VALUES (?, 'tg', 'x', 'bogus')",
            (user_id,),
        )


# --- T1.4 requests / jobs ----------------------------------------------------
def test_request_job_round_trip(conn: sqlite3.Connection) -> None:
    user_id = _new_user(conn)
    req_id = _new_request(conn, "20260614120000", user_id)
    conn.execute(
        "INSERT INTO request_details (request_id, content, source, routed_by) "
        "VALUES (?, 'more info', 'user', 'pm')",
        (req_id,),
    )
    job_id = _new_job(conn, req_id, "task")
    conn.commit()

    row = conn.execute(
        "SELECT j.kind, r.code FROM jobs j JOIN requests r ON r.id = j.request_id WHERE j.id = ?",
        (job_id,),
    ).fetchone()
    assert row["kind"] == "task"
    assert row["code"] == "20260614120000"


def test_requests_code_is_unique(conn: sqlite3.Connection) -> None:
    user_id = _new_user(conn)
    _new_request(conn, "20260614120000", user_id)
    with pytest.raises(sqlite3.IntegrityError):
        _new_request(conn, "20260614120000", user_id)


def test_jobs_reject_unknown_kind(conn: sqlite3.Connection) -> None:
    user_id = _new_user(conn)
    req_id = _new_request(conn, "20260614120001", user_id)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO jobs (request_id, kind) VALUES (?, 'nope')", (req_id,))


# --- T1.5 plans / phases / tasks / steps -------------------------------------
def test_plan_phase_task_tree(conn: sqlite3.Connection) -> None:
    user_id = _new_user(conn)
    req_id = _new_request(conn, "20260614120100", user_id)
    job_id = _new_job(conn, req_id)
    plan_id = conn.execute("INSERT INTO plans (job_id) VALUES (?)", (job_id,)).lastrowid
    phase_id = conn.execute(
        "INSERT INTO phases (plan_id, idx, title) VALUES (?, 0, 'phase one')",
        (plan_id,),
    ).lastrowid
    parent_task = conn.execute(
        "INSERT INTO plan_tasks (phase_id, title) VALUES (?, 'parent')",
        (phase_id,),
    ).lastrowid
    conn.execute(
        "INSERT INTO plan_tasks (phase_id, parent_task_id, title) VALUES (?, ?, 'child')",
        (phase_id, parent_task),
    )
    conn.execute(
        "INSERT INTO steps (job_id, plan_task_id, idx, skill_name, status) "
        "VALUES (?, ?, 0, 'memory.search', 'done')",
        (job_id, parent_task),
    )
    conn.commit()

    children = conn.execute(
        "SELECT title FROM plan_tasks WHERE parent_task_id = ?", (parent_task,)
    ).fetchall()
    assert [c["title"] for c in children] == ["child"]
    step = conn.execute("SELECT skill_name FROM steps WHERE job_id = ?", (job_id,)).fetchone()
    assert step["skill_name"] == "memory.search"


# --- T1.6 roles / audit ------------------------------------------------------
def test_role_messages_causation_chain(conn: sqlite3.Connection) -> None:
    user_id = _new_user(conn)
    req_id = _new_request(conn, "20260614120200", user_id)
    first = conn.execute(
        "INSERT INTO role_messages (request_id, from_role, to_role, action) "
        "VALUES (?, 'PM', 'Boss', 'route_request')",
        (req_id,),
    ).lastrowid
    second = conn.execute(
        "INSERT INTO role_messages (request_id, from_role, to_role, action, causation_id) "
        "VALUES (?, 'Boss', 'Analyzer', 'analyze', ?)",
        (req_id, first),
    ).lastrowid
    conn.execute(
        "INSERT INTO ai_calls (request_id, role_message_id, role, model_id, validation_status) "
        "VALUES (?, ?, 'Analyzer', 'openai/gpt-4o', 'ok')",
        (req_id, second),
    )
    conn.execute(
        "INSERT INTO audit_log (actor, action, target) VALUES ('role', 'analyze', ?)",
        (str(req_id),),
    )
    conn.commit()

    chained = conn.execute(
        "SELECT causation_id FROM role_messages WHERE id = ?", (second,)
    ).fetchone()
    assert chained["causation_id"] == first


def test_agents_reject_bad_scope(conn: sqlite3.Connection) -> None:
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO agents (role, scope) VALUES ('PM', 'galaxy')")


# --- T1.7 memory / library ---------------------------------------------------
def test_memory_tag_and_final_report(conn: sqlite3.Connection) -> None:
    user_id = _new_user(conn)
    req_id = _new_request(conn, "20260614120300", user_id)
    job_id = _new_job(conn, req_id)
    mem_id = conn.execute(
        "INSERT INTO memories (user_id, kind, entity_key, content, retention_class) "
        "VALUES (?, 'fact', 'location:home', 'Paris', 'long')",
        (user_id,),
    ).lastrowid
    conn.execute("INSERT INTO memory_tags (memory_id, tag) VALUES (?, 'pref')", (mem_id,))
    conn.execute(
        "INSERT INTO final_reports (request_id, job_id, brief_description, outcome) "
        "VALUES (?, ?, 'did the thing', 'delivered')",
        (req_id, job_id),
    )
    conn.commit()

    tag = conn.execute("SELECT tag FROM memory_tags WHERE memory_id = ?", (mem_id,)).fetchone()
    assert tag["tag"] == "pref"
    fr = conn.execute(
        "SELECT outcome FROM final_reports WHERE request_id = ?", (req_id,)
    ).fetchone()
    assert fr["outcome"] == "delivered"


def test_memory_superseded_chain(conn: sqlite3.Connection) -> None:
    user_id = _new_user(conn)
    old = conn.execute(
        "INSERT INTO memories (user_id, entity_key, content, state) "
        "VALUES (?, 'location:home', 'Paris', 'archived')",
        (user_id,),
    ).lastrowid
    new = conn.execute(
        "INSERT INTO memories (user_id, entity_key, content) VALUES (?, 'location:home', 'Lyon')",
        (user_id,),
    ).lastrowid
    conn.execute("UPDATE memories SET superseded_by = ? WHERE id = ?", (new, old))
    conn.commit()

    row = conn.execute("SELECT superseded_by FROM memories WHERE id = ?", (old,)).fetchone()
    assert row["superseded_by"] == new


# --- T1.8 schedules / reports ------------------------------------------------
def test_schedule_and_report_round_trip(conn: sqlite3.Connection) -> None:
    user_id = _new_user(conn)
    conn.execute(
        "INSERT INTO user_interests (user_id, topic, weight) VALUES (?, 'ai', 0.8)",
        (user_id,),
    )
    sched_id = conn.execute(
        "INSERT INTO schedules (kind, schedule_cron, enabled) VALUES ('digest', '0 8 * * *', 1)"
    ).lastrowid
    conn.execute(
        "INSERT INTO reports (user_id, schedule_id, title) VALUES (?, ?, 'Daily digest')",
        (user_id, sched_id),
    )
    conn.commit()

    row = conn.execute(
        "SELECT s.kind FROM reports r JOIN schedules s ON s.id = r.schedule_id WHERE r.user_id = ?",
        (user_id,),
    ).fetchone()
    assert row["kind"] == "digest"
