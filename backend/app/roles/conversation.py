"""Conversation context — the prior turn, for follow-up continuity (design-spec §6C).

When a user sends a follow-up, two things must happen (and both need the **prior
turn**):

  1. decide whether the message **continues the last request** or starts a new
     one (the Analyzer's ``belongs`` judgment, given this context); and
  2. resolve **references to earlier info** — "the plan", "that URL", "it" — by
     surfacing what was last asked + answered (and any drafted plan) so the model
     can ground them.

This module assembles that context deterministically from the DB and renders it
as a compact, **id-free** prompt block (no internal request/job ids leave the
machine — only user-authored text + what we already told the user).
"""

from __future__ import annotations

from dataclasses import dataclass, field

from app.storage.repos import plans as plans_repo
from app.storage.repos import requests as requests_repo
from app.storage.repos import role_messages as role_messages_repo
from app.storage.repos.requests import Request

# Keep the context compact so it never dominates the prompt or leaks long blobs.
_MAX_ANSWER_CHARS = 600
_MAX_PHASES = 12


@dataclass(frozen=True)
class ConversationContext:
    """The prior turn for one user: what they last asked, and what we answered."""

    request: Request
    last_answer: str | None = None
    plan_outline: list[str] = field(default_factory=list)


def build(conn, prior: Request | None) -> ConversationContext | None:
    """Assemble the context for the user's ``prior`` request (``None`` → no context)."""
    if prior is None:
        return None
    last_answer = role_messages_repo.get_last_answer_text(conn, prior.id)
    outline: list[str] = []
    job = requests_repo.get_job_for_request(conn, prior.id)
    if job is not None:
        plan = plans_repo.get_plan_for_job(conn, job.id)
        if plan is not None:
            outline = [phase.title for phase in plans_repo.list_phases(conn, plan.id)]
    return ConversationContext(request=prior, last_answer=last_answer, plan_outline=outline)


def load(conn, user_id: int | None, *, exclude_request_id: int | None = None):
    """Build the context from the user's most recent active request (the prior turn)."""
    if user_id is None:
        return None
    prior = requests_repo.get_latest_active_request(
        conn, user_id, exclude_request_id=exclude_request_id
    )
    return build(conn, prior)


def render(ctx: ConversationContext | None) -> str:
    """Render the context as a compact, **id-free** prompt block (``""`` when none)."""
    if ctx is None:
        return ""
    lines = ["Conversation so far (the user's previous turn):"]
    if ctx.request.title:
        lines.append(f"- previously asked: {ctx.request.title}")
    if ctx.last_answer:
        answer = ctx.last_answer.strip()
        if len(answer) > _MAX_ANSWER_CHARS:
            answer = answer[:_MAX_ANSWER_CHARS].rstrip() + "…"
        lines.append(f"- you previously answered: {answer}")
    if ctx.plan_outline:
        phases = ", ".join(ctx.plan_outline[:_MAX_PHASES])
        lines.append(f"- a plan was drafted with these phases: {phases}")
    lines.append(
        "If the new message refers back to the above (e.g. 'the plan', 'that "
        "link', 'it'), resolve the reference using this context."
    )
    return "\n".join(lines)
