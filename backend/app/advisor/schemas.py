"""Advisor output contracts (design-spec §6D, §7; implementation-plan P3).

Every advisor method returns one of these **pydantic-validated** schemas — the
typed verdict a role turns into an envelope payload (§6D). The model proposes;
deterministic code validates into these types before anything acts on them.

The classification enums are the spec's job dimensions (§5/§6A):
    kind        ∈ {ask, task, feature}
    clarity     ∈ {clear, unclear}
    complexity  ∈ {simple, complex}
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

Kind = Literal["ask", "task", "feature"]
Clarity = Literal["clear", "unclear"]
Complexity = Literal["simple", "complex"]


class Triage(BaseModel):
    """Junior Worker's cheap first-pass classification (template `triage.classify`)."""

    kind: Kind
    clarity: Clarity
    complexity: Complexity
    confidence: float = Field(ge=0.0, le=1.0)
    rationale: str


class PlanDraft(BaseModel):
    """Minimal plan placeholder for P3. Full plan drafting lands in P6 (T6.1)."""

    phases: list[str] = Field(default_factory=list)


class Analysis(BaseModel):
    """Analyzer's authoritative verdict (template `analyzer.analyze`, §6D)."""

    belongs: bool
    kind: Kind
    clarity: Clarity
    complexity: Complexity
    confidence: float = Field(ge=0.0, le=1.0)
    rationale: str
    plan: PlanDraft | None = None
    clarify: list[str] | None = None


class Source(BaseModel):
    """A cited source backing an answer (a memory id, a URL, etc.) — §7.1, §8.11."""

    ref: str = Field(..., min_length=1)
    title: str | None = None
    url: str | None = None
    snippet: str | None = None


class AnswerDraft(BaseModel):
    """Junior Worker's drafted answer (template `junior.answer`, §6D).

    An answer **must** carry at least one citation — a zero-citation draft is
    rejected at validation (§8.11).
    """

    answer: str = Field(..., min_length=1)
    citations: list[Source] = Field(..., min_length=1)
    confidence: float = Field(ge=0.0, le=1.0)
