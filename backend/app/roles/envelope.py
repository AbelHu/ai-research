"""The `RoleMessage` envelope + the `Role`/`Action` vocabularies (design-spec §6D).

Each hand-off between roles is a typed envelope carrying an **`action`** verb and
a typed `payload`. The **Boss reads the `action`** and schedules the next role;
**the model never sets `action`** — deterministic code maps a validated AI
verdict to the next verb, keeping AI out of the control path.

> **Id invariant (§6D).** `request_id`/`job_id` are always the **DB foreign keys**
> (`requests.id` / `jobs.id`); the user-facing `/req` handle is `requests.code`.
"""

from __future__ import annotations

from enum import Enum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class Role(str, Enum):
    """The agent roles that exchange envelopes (§6A)."""

    user = "user"
    pm = "pm"
    boss = "boss"
    analyzer = "analyzer"
    junior = "junior"
    company_expert = "company_expert"
    senior_worker = "senior_worker"
    plan_expert = "plan_expert"
    librarian = "librarian"


class Action(str, Enum):
    """The verbs the Boss routes on (§6D action vocabulary)."""

    route_request = "route_request"
    analyze = "analyze"
    analysis_done = "analysis_done"
    answer_ask = "answer_ask"
    ask_done = "ask_done"
    review_plan = "review_plan"
    review_phase = "review_phase"
    review_final = "review_final"
    approved = "approved"
    declined = "declined"
    run_phase = "run_phase"
    run_task = "run_task"
    task_done = "task_done"
    phase_done = "phase_done"
    phase_report = "phase_report"
    final_report = "final_report"
    archive = "archive"
    archived = "archived"
    deliver = "deliver"
    progress = "progress"
    clarify = "clarify"
    undo_append = "undo_append"


EnvelopeStatus = Literal["queued", "in_progress", "done", "failed"]


class RoleMessage(BaseModel):
    """A single typed hand-off between two roles (§6D).

    ``action`` is constrained to the `Action` vocabulary so a malformed verb is
    rejected at construction — routing can never act on an unknown verb. The
    DB ``id``/``created_at`` are assigned on persistence (the `role_messages`
    repo) and stay ``None`` for an in-memory envelope.
    """

    model_config = ConfigDict(extra="forbid")

    request_id: int
    from_role: Role
    to_role: Role
    action: Action
    payload: dict = Field(default_factory=dict)
    job_id: int | None = None
    template: str | None = None
    status: EnvelopeStatus = "queued"
    causation_id: int | None = None
    id: int | None = None
    created_at: str | None = None
