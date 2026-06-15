"""The Junior Worker — simple-ask path (design-spec §6A, §6D; implementation-plan T4.5).

For a clear simple ask the Junior Worker handles it end-to-end:

  1. run **`memory.search`** through the skill runtime (records a `steps` row);
  2. draft a **validated** answer from the hits via the advisor (`Advisor.answer`)
     — the draft must carry ≥1 citation and any cited URL is verified (§7.1);
  3. emit `ask_done` to the Boss carrying the answer.

The AI only drafts; deterministic code runs the skill, validates the answer, and
forms the envelope (AI stays out of the control path).
"""

from __future__ import annotations

from dataclasses import dataclass

import app.skills  # noqa: F401  -- ensure @skill registration (memory.search)
from app.advisor.schemas import AnswerDraft
from app.advisor.wrapper import Advisor, AdvisorValidationError
from app.roles.envelope import Action, Role, RoleMessage
from app.skills import runtime
from app.skills.context import SkillContext

# The Junior reads memory; it never writes (local_write/external need a gate).
_JUNIOR_PERMISSIONS = frozenset({"memory.read"})

# Fields of a memory hit safe to show the model. Internal identifiers (the DB
# `id`) and lifecycle bookkeeping (`state`, `use_count`, TTL fields) are kept out
# of the prompt; the model only needs the content + an opaque citation ref.
_HIT_FIELDS_FOR_MODEL = ("content", "summary", "tags")


def _hits_for_model(hits: list) -> list[dict]:
    """Render search hits for the prompt with an **opaque** citation ref.

    The model never sees a memory's DB id — each hit gets a per-call token
    (``m1``, ``m2``, …) it can cite instead. Deterministic code keeps the real
    id; only the content + opaque ref leave the machine (privacy: no internal
    ids in prompts).
    """
    rendered: list[dict] = []
    for index, hit in enumerate(hits, start=1):
        data = hit.model_dump()
        item = {"ref": f"m{index}"}
        item.update({k: data[k] for k in _HIT_FIELDS_FOR_MODEL if data.get(k) is not None})
        rendered.append(item)
    return rendered


@dataclass(frozen=True)
class JuniorResult:
    answer: AnswerDraft | None  # None when no citable answer could be produced
    envelope: RoleMessage  # the `ask_done` hand-off to the Boss


def answer_ask(
    conn,
    advisor: Advisor,
    card: dict,
    *,
    user_id: int | None,
    job_id: int,
    search_limit: int = 10,
) -> JuniorResult:
    """Answer a simple ask: search memory → validated answer → `ask_done` (§6D)."""
    ctx = SkillContext(
        user_id=user_id if user_id is not None else 0,
        conn=conn,
        permissions=_JUNIOR_PERMISSIONS,
        job_id=job_id,
    )
    search = runtime.execute("memory.search", {"query": card["text"], "limit": search_limit}, ctx)
    hits = _hits_for_model(search.value.hits)

    try:
        draft: AnswerDraft | None = advisor.answer(
            text=card["text"],
            hits=hits,
            request_id=card["request_id"],
            job_id=job_id,
        )
    except AdvisorValidationError:
        # The advisor escalated: the model produced no usable answer object even
        # after one repair (a genuine schema failure). Citation/URL checks are
        # non-fatal and never land here. Degrade gracefully: emit `ask_done`
        # with no answer so the PM tells the user honestly, rather than letting
        # the escalation crash the run.
        draft = None

    envelope = RoleMessage(
        request_id=card["request_id"],
        job_id=job_id,
        from_role=Role.junior,
        to_role=Role.boss,
        action=Action.ask_done,
        payload={
            "answer": draft.model_dump() if draft is not None else None,
            "unanswered": draft is None,
            "card": card,
        },
        template="junior.answer@v1",
    )
    return JuniorResult(answer=draft, envelope=envelope)
