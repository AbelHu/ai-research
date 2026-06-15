"""Prompt privacy: internal ids must never reach the AI model.

Internal identifiers (request id + `/req` code, job id, plan/phase/task ids,
memory ids) are deterministic plumbing — the model never needs them. These
tests capture the *actual* prompt strings sent to the provider across the role
flows and assert no internal id leaks into them. The ids still flow through the
deterministic envelope + the `ai_calls` audit row (DB), just not the prompt.
"""

from __future__ import annotations

import json

import pytest

from app.advisor.providers import CompletionRequest, CompletionResponse, EmbedRequest, EmbedResponse
from app.advisor.schemas import PhaseSpec, PlanSpec, TaskSpec
from app.advisor.wrapper import Advisor
from app.roles import analyzer, company_expert, junior
from app.roles.envelope import Role
from app.storage.db import connect
from app.storage.migrations import migrate
from app.storage.repos import memories as memories_repo
from app.storage.repos import plans as plans_repo
from app.storage.repos import requests as requests_repo


class CapturingProvider:
    """Records every prompt sent, and replays a canned reply."""

    def __init__(self, reply: str) -> None:
        self.reply = reply
        self.model = "capture"
        self.prompts: list[str] = []

    def complete(self, req: CompletionRequest) -> CompletionResponse:
        # The user-turn prompt is the first message's content.
        self.prompts.append(req.messages[0]["content"])
        return CompletionResponse(
            text=self.reply,
            model=self.model,
            raw={"choices": [{"message": {"content": self.reply}}]},
        )

    def embed(self, req: EmbedRequest) -> EmbedResponse:  # pragma: no cover - unused
        return EmbedResponse(vectors=[[0.0]], model=self.model)


@pytest.fixture
def db():
    conn = connect()
    migrate(conn)
    try:
        yield conn
    finally:
        conn.close()


def _all_prompts_text(provider: CapturingProvider) -> str:
    return "\n".join(provider.prompts)


def test_analyzer_prompt_has_no_request_id_or_code(db) -> None:
    # Burn a few requests so the id is a distinctive multi-digit number.
    for _ in range(11):
        requests_repo.create_request(db)
    req = requests_repo.create_request(db, title="compare vendors")
    card = {
        "request_id": req.id,
        "request_code": req.code,
        "title": req.title,
        "text": "compare three vendors and recommend one",
        "append": False,
    }
    analysis_reply = json.dumps(
        {
            "belongs": True,
            "kind": "ask",
            "clarity": "clear",
            "complexity": "simple",
            "confidence": 0.9,
            "rationale": "ok",
        }
    )
    provider = CapturingProvider(analysis_reply)
    advisor = Advisor(resolve_provider=lambda role: provider, conn=db)

    analyzer.analyze(db, advisor, card)

    prompt = _all_prompts_text(provider)
    assert req.code not in prompt  # the /req handle never leaves the machine
    assert f"code: {req.code}" not in prompt
    assert str(req.id) not in prompt  # the DB request id (distinctive, 2+ digits)
    # The user content the model *does* need is present.
    assert "compare three vendors" in prompt


def test_expert_review_prompt_has_no_plan_or_phase_id(db) -> None:
    req = requests_repo.create_request(db, title="vendor compare")
    job = requests_repo.create_job(db, request_id=req.id, kind="task", complexity="complex")
    spec = PlanSpec(phases=[PhaseSpec(title="Research phase", tasks=[TaskSpec(title="gather")])])
    plan = plans_repo.create_plan_from_spec(db, job_id=job.id, spec=spec)

    approve = json.dumps({"decision": "approve", "comments": []})
    provider = CapturingProvider(approve)
    advisor = Advisor(resolve_provider=lambda role: provider, conn=db)

    # Review the plan, then a resolved phase.
    company_expert.review_plan(db, advisor, plan, request_id=req.id)
    phase = plans_repo.list_phases(db, plan.id)[0]
    # drive the phase to Resolved so it can be reviewed
    plans_repo.set_phase_status(db, phase.id, "Active", actor=Role.boss)
    plans_repo.set_phase_status(db, phase.id, "InProgress", actor=Role.senior_worker)
    for task in plans_repo.list_tasks(db, phase.id):
        plans_repo.set_task_status(db, task.id, "InProgress", actor=Role.senior_worker)
        plans_repo.set_task_status(db, task.id, "Resolved", actor=Role.senior_worker)
    plans_repo.set_phase_status(db, phase.id, "Resolved", actor=Role.plan_expert)
    company_expert.review_phase(db, advisor, plans_repo.get_phase(db, phase.id), request_id=req.id)

    prompt = _all_prompts_text(provider)
    assert f"#{plan.id}" not in prompt  # no "plan #<id>"
    assert f"#{phase.id}" not in prompt  # no "phase #<id>"
    assert "plan #" not in prompt
    assert "phase #" not in prompt
    # The descriptive title the model needs is present.
    assert "Research phase" in prompt


def test_answer_prompt_has_no_memory_db_id(db) -> None:
    req = requests_repo.create_request(db, title="capital of France")
    job = requests_repo.create_job(db, request_id=req.id, kind="ask", complexity="simple")
    # Distinctive memory id by burning a few rows first.
    for _ in range(20):
        memories_repo.create_memory(db, content="filler about other topics")
    mem = memories_repo.create_memory(db, content="the capital of France is Paris")

    answer_reply = json.dumps(
        {
            "answer": "Paris.",
            "citations": [{"ref": "m1", "snippet": "capital of France is Paris"}],
            "confidence": 0.9,
        }
    )
    provider = CapturingProvider(answer_reply)
    advisor = Advisor(resolve_provider=lambda role: provider, conn=db)
    card = {
        "request_id": req.id,
        "request_code": req.code,
        "title": req.title,
        "text": "capital of France is Paris",
        "append": False,
    }

    junior.answer_ask(db, advisor, card, user_id=None, job_id=job.id)

    prompt = _all_prompts_text(provider)
    # The memory content reaches the model, but its DB id + an "id" field do not.
    assert "capital of France is Paris" in prompt
    assert '"id"' not in prompt
    assert str(mem.id) not in prompt
    # An opaque per-call citation ref is offered instead.
    assert '"ref": "m1"' in prompt or '"ref":"m1"' in prompt


def test_hits_for_model_strips_internal_fields(db) -> None:
    from app.roles.junior import _hits_for_model
    from app.skills.memory import MemoryHit

    hits = [
        MemoryHit(id=42, content="c", summary="s", state="active", use_count=7, tags=["x"]),
    ]
    rendered = _hits_for_model(hits)
    assert rendered == [{"ref": "m1", "content": "c", "summary": "s", "tags": ["x"]}]
    # No id / state / use_count / TTL fields exposed.
    blob = json.dumps(rendered)
    assert "42" not in blob
    assert "use_count" not in blob
    assert "state" not in blob
