"""Recovery: rebuild an ask's full state from the DB after a restart (T4.7).

The durable truth is folders + DB (§6A). After a simple ask runs, every row it
produced — request, job, steps, role_messages, ai_calls — must be re-readable
from a freshly reopened connection, with the envelope causation chain intact.
"""

from __future__ import annotations

import json

from app.advisor.wrapper import Advisor
from app.roles.control import ensure_owner, run_ask
from app.storage.db import connect
from app.storage.migrations import migrate
from app.storage.repos import ai_calls as ai_calls_repo
from app.storage.repos import memories as memories_repo
from app.storage.repos import requests as requests_repo
from app.storage.repos import role_messages as role_messages_repo
from app.storage.repos import steps as steps_repo
from tests.fakes import FakeProvider

ANALYSIS_ASK = json.dumps(
    {
        "belongs": True,
        "kind": "ask",
        "clarity": "clear",
        "complexity": "simple",
        "confidence": 0.95,
        "rationale": "direct question",
    }
)
ANSWER = json.dumps(
    {
        "answer": "Paris is the capital of France.",
        "citations": [{"ref": "memory:1"}],
        "confidence": 0.95,
    }
)


def _advisor(conn) -> Advisor:
    providers = {"planner": FakeProvider(ANALYSIS_ASK), "drafter": FakeProvider(ANSWER)}
    return Advisor(resolve_provider=lambda role: providers[role], conn=conn)


def test_ask_state_recovers_after_restart(tmp_path) -> None:
    db_path = tmp_path / "app.db"

    # --- first process: run the ask against a file-backed DB ---
    conn = connect(db_path)
    migrate(conn)
    memories_repo.create_memory(conn, content="the capital of France is Paris")
    user_id = ensure_owner(conn)
    outcome = run_ask(conn, _advisor(conn), "what is the capital of France?", user_id=user_id)
    request_id = outcome.request.id
    job_id = outcome.job_id
    conn.close()  # simulate shutdown

    # --- second process: reopen the same DB, rebuild state, no re-run ---
    conn2 = connect(db_path)
    try:
        request = requests_repo.get_request(conn2, request_id)
        assert request is not None
        assert request.code == outcome.request.code

        job = requests_repo.get_job_for_request(conn2, request_id)
        assert job is not None and job.id == job_id and job.kind == "ask"

        # The envelope chain is intact and ordered, with linked causation.
        msgs = role_messages_repo.list_role_messages(conn2, request_id)
        assert [m["action"] for m in msgs] == [
            "route_request",
            "analyze",
            "analysis_done",
            "answer_ask",
            "ask_done",
            "deliver",
        ]
        ids = [m["id"] for m in msgs]
        assert [m["causation_id"] for m in msgs][1:] == ids[:-1]

        # The skill step and both AI calls survived the restart.
        assert [s["skill_name"] for s in steps_repo.list_steps(conn2, job_id)] == ["memory.search"]
        assert len(ai_calls_repo.list_ai_calls(conn2, request_id)) == 2

        # The envelope rebuilds into a typed RoleMessage (round-trip).
        deliver = role_messages_repo.envelope_from_row(msgs[-1])
        assert deliver.payload["answer"]["answer"].startswith("Paris")
    finally:
        conn2.close()
