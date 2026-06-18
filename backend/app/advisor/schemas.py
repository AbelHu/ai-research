"""Advisor output contracts (design-spec §6D, §7; implementation-plan P3).

Every advisor method returns one of these **pydantic-validated** schemas — the
typed verdict a role turns into an envelope payload (§6D). The model proposes;
deterministic code validates into these types before anything acts on them.

Each model is the typed form of its template's declared response schema (the
*template requirement*). All models set ``extra="forbid"`` so a reply carrying
**extra/invented fields is rejected as a hallucination** — a role acts only on a
reply that conforms exactly to the template requirement (§6D, §7).

The classification enums are the spec's job dimensions (§5/§6A):
    kind        ∈ {ask, task, feature}
    clarity     ∈ {clear, unclear}
    complexity  ∈ {simple, complex}
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

Kind = Literal["ask", "task", "feature"]
Clarity = Literal["clear", "unclear"]
Complexity = Literal["simple", "complex"]
# The work domain the Analyzer assigns, used by deterministic code to gate which
# tools a request may use (§8.6). ``coding`` work is self-contained against local
# context, so it skips external web research; ``general`` is the safe default.
Domain = Literal["coding", "research", "general"]


class _Strict(BaseModel):
    """Base for advisor contracts: reject any field outside the template schema."""

    model_config = ConfigDict(extra="forbid")


class Triage(_Strict):
    """Junior Worker's cheap first-pass classification (template `triage.classify`)."""

    kind: Kind
    clarity: Clarity
    complexity: Complexity
    confidence: float = Field(ge=0.0, le=1.0)
    rationale: str


class PlanDraft(_Strict):
    """Minimal plan placeholder for P3. Full plan drafting lands in P6 (T6.1)."""

    phases: list[str] = Field(default_factory=list)


class TaskSpec(_Strict):
    """One task in a drafted plan (template `analyzer.plan`, §6B).

    ``depends_on`` holds the 0-based indices of **earlier sibling tasks in the
    same phase** that must be ``Resolved`` first; ``run_mode`` lets independent
    tasks run in parallel. Validated against the phase on persistence.
    """

    title: str = Field(..., min_length=1)
    depends_on: list[int] = Field(default_factory=list)
    run_mode: Literal["serial", "parallel"] = "serial"


class PhaseSpec(_Strict):
    """One phase in a drafted plan: an ordered list of tasks (§6B)."""

    title: str = Field(..., min_length=1)
    tasks: list[TaskSpec] = Field(default_factory=list)


class PlanSpec(_Strict):
    """The Analyzer's full drafted plan — phases → tasks (+ deps) (§6B, T6.1).

    The richer drafting contract behind a complex job, distinct from the
    lightweight `PlanDraft` hint carried inline on `Analysis`. Requires at least
    one phase; code validates dependency indices when it persists the plan.
    """

    phases: list[PhaseSpec] = Field(..., min_length=1)
    # Explicit, checkable completion criteria for the goal (§6B; P3). Optional so
    # older plan replies still validate; when present, the runner verifies them
    # before reporting the job completed.
    success_criteria: list[str] = Field(default_factory=list)


class CriterionResult(_Strict):
    """One success criterion's verification outcome (template ``expert.verify``)."""

    criterion: str = Field(..., min_length=1)
    met: bool
    note: str = ""


class CriteriaReport(_Strict):
    """Goal-criteria verification verdict before a job is reported done (§6B; P3).

    The Plan Expert advises whether each explicit success criterion is satisfied
    by the work done; deterministic code decides whether to complete the job or
    escalate/replan. ``all_met`` is the single gate code reads.
    """

    results: list[CriterionResult] = Field(default_factory=list)
    all_met: bool


class Analysis(_Strict):
    """Analyzer's authoritative verdict (template `analyzer.analyze`, §6D)."""

    belongs: bool
    kind: Kind
    clarity: Clarity
    complexity: Complexity
    confidence: float = Field(ge=0.0, le=1.0)
    rationale: str
    plan: PlanDraft | None = None
    clarify: list[str] | None = None
    # Advisory work domain (default ``general``) that deterministic code maps to
    # an allowed tool set (§8.6). Optional so older replies still validate.
    domain: Domain = "general"


class Source(_Strict):
    """A cited source backing an answer (a memory id, a URL, etc.) — §7.1, §8.11."""

    ref: str = Field(..., min_length=1)
    title: str | None = None
    url: str | None = None
    snippet: str | None = None


class AnswerDraft(_Strict):
    """Junior Worker's drafted answer (template `junior.answer`, §6D).

    ``citations`` is **required but may be empty**: an answer grounded in
    provided sources should cite them, but an answer from the model's own
    knowledge — or an honest "I don't have a source for this" — is allowed and
    **not rejected**. The worker validates citations as a *non-fatal annotation*
    (logged), so it always returns the model's templated answer rather than
    failing the request (owner policy, see `Advisor.answer`).
    """

    answer: str = Field(..., min_length=1)
    citations: list[Source]
    confidence: float = Field(ge=0.0, le=1.0)


class Trait(_Strict):
    """A user 'character' extracted during review (habit/liking/location…) — §6D."""

    key: str = Field(..., min_length=1)
    value: str = Field(..., min_length=1)
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)


class Verdict(_Strict):
    """A Company Expert sign-off verdict (template `expert.review`, §6D).

    ``decision`` drives the deterministic next verb (approve/decline); code, not
    the model, applies the status change and bounds the decline loop (§6B).
    """

    decision: Literal["approve", "decline"]
    comments: list[str] = Field(default_factory=list)
    characters: list[Trait] | None = None


class ProposedAction(_Strict):
    """A Senior Worker's proposed next skill call (template `worker.next_action`).

    The advisor never runs a skill — it returns this **validated proposal** that
    deterministic code re-validates against the catalog + policy gate before the
    skill runtime executes it (§8.4). ``done`` lets the worker signal the task is
    complete instead of proposing another action.
    """

    skill: str = Field(..., min_length=1)
    params: dict = Field(default_factory=dict)
    rationale: str = ""
    done: bool = False


class GeneratedSkill(_Strict):
    """A feature job's generated reusable skill (template `coder.generate`, §5/§6B).

    The model proposes the **code** for a new ``@skill``; deterministic code
    writes it **inert** (never executed) under ``app/skills/generated/<job>/``
    and gates activation on user confirmation (`confirm_generated_code`). The
    ``skill_name`` must be ``generated.*`` so a generated skill can never shadow a
    built-in; ``module_filename`` is a bare ``*.py`` name (no path separators).
    """

    skill_name: str = Field(..., pattern=r"^generated\.[a-z0-9_]+$")
    module_filename: str = Field(..., pattern=r"^[a-z0-9_]+\.py$")
    code: str = Field(..., min_length=1)
    rationale: str = ""


class GeneratedFile(_Strict):
    """One file in a generated skill bundle — a skill module or a test module."""

    filename: str = Field(..., pattern=r"^[a-z0-9_]+\.py$")
    code: str = Field(..., min_length=1)


class GeneratedSkillBundle(_Strict):
    """A feature job's generated **multi-file** skill bundle (template `coder.bundle`).

    ``files`` are the skill module(s) that get imported on activation; optional
    ``test_files`` are pytest modules used only to **validate** the bundle in the
    Coder sandbox — they are never shipped or imported on activation. The model
    proposes the code; deterministic code writes it inert and validates it in a
    sandbox before offering activation (§5/§6B; P2/P3).
    """

    files: list[GeneratedFile] = Field(..., min_length=1)
    test_files: list[GeneratedFile] = Field(default_factory=list)
    rationale: str = ""
