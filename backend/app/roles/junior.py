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

import json
from dataclasses import dataclass

import app.skills  # noqa: F401  -- ensure @skill registration (memory/web/data)
from app.advisor.schemas import AnswerDraft
from app.advisor.wrapper import Advisor, AdvisorValidationError
from app.roles.envelope import Action, Role, RoleMessage
from app.skills import runtime
from app.skills.context import SkillContext
from app.skills.policy import PermissionDenied
from app.skills.registry import catalog
from app.skills.runtime import InvalidParams, UnknownSkill

# The Junior reads memory + read-only web/data tools; it never writes.
_JUNIOR_PERMISSIONS = frozenset({"memory.read", "web.read", "data.read"})

# Read-only tools the Junior may call to gather live/web context when local
# memory has nothing. `web.search` is offered only when configured (a Tavily
# key), so a key-free machine still gets `data.weather` + `web.fetch`.
_RESEARCH_TOOLS_BASE = ("data.weather", "web.fetch")
_MAX_RESEARCH_STEPS = 2

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


def _research_tool_names() -> set[str]:
    """Read tools the Junior may use now (adds `web.search` only when configured)."""
    from app.config.settings import get_settings

    names = set(_RESEARCH_TOOLS_BASE)
    try:
        if get_settings().tavily_api_key is not None:
            names.add("web.search")
    except Exception:  # noqa: BLE001 - config trouble must never block answering
        pass
    return names


def _finding_from_result(skill_name: str, value) -> list[dict]:
    """Render a read-tool result as answer 'hits' (``ref`` = url so the model cites it)."""
    data = value.model_dump()
    if not data.get("ok"):
        return []
    if skill_name == "data.weather":
        loc = data.get("location") or "the location"
        lines = [
            f"{d['date']}: {d['summary']}, {d.get('temp_min_c')}–{d.get('temp_max_c')}°C, "
            f"precip {d.get('precip_prob_pct')}%"
            for d in data.get("days", [])
        ]
        body = f"Weather forecast for {loc}, {data.get('country')}:\n" + "\n".join(lines)
        return [
            {"ref": data.get("source_url"), "title": f"Weather forecast for {loc}", "content": body}
        ]
    if skill_name == "web.search":
        findings: list[dict] = []
        if data.get("answer"):
            findings.append(
                {"ref": "web:search", "title": "Web search summary", "content": data["answer"]}
            )
        for hit in data.get("results", []):
            findings.append(
                {"ref": hit.get("url"), "title": hit.get("title"), "content": hit.get("snippet")}
            )
        return findings
    if skill_name == "web.fetch":
        return [
            {
                "ref": data.get("url"),
                "title": data.get("title"),
                "content": (data.get("text") or "")[:2000],
            }
        ]
    return []


def _research(
    conn, advisor: Advisor, text: str, ctx: SkillContext, *, request_id: int, job_id: int
) -> list[dict]:
    """Bounded read-tool loop: gather web/live context when memory had nothing (§6A).

    The advisor proposes which read tool to call (reusing ``next_action`` over a
    restricted catalog); deterministic code runs it and renders the result as
    citeable findings. If the model can't propose a valid tool (or says it's
    done), we fall through and let it answer from its own knowledge.
    """
    allowed = _research_tool_names()
    if not allowed:
        return []
    findings: list[dict] = []
    progress = ""
    for _ in range(_MAX_RESEARCH_STEPS):
        try:
            action = advisor.next_action(
                goal=text,
                catalog=json.dumps(catalog(allowed), ensure_ascii=False),
                progress=progress,
                request_id=request_id,
                job_id=job_id,
            )
        except AdvisorValidationError:
            break
        if action.done or action.skill not in allowed:
            break
        try:
            result = runtime.execute(action.skill, action.params, ctx)
        except (UnknownSkill, InvalidParams, PermissionDenied):
            break
        new = _finding_from_result(action.skill, result.value) if result.value else []
        if new:
            findings.extend(new)
            # One good finding answers a simple ask — stop rather than spending
            # another (possibly metered, e.g. web.search) call. Multi-source
            # gathering is the planned-job path, not the fast simple-ask path.
            break
        # The tool returned nothing useful; note it so the model can try a
        # different tool on the next (bounded) step instead of repeating it.
        progress += f"Tried {action.skill}; no useful result. "
    return findings


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

    # When local memory has nothing relevant, gather live/web context (§6A) so a
    # question needing current data (weather, etc.) can still be answered + cited.
    if not hits:
        hits = _research(
            conn, advisor, card["text"], ctx, request_id=card["request_id"], job_id=job_id
        )

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
