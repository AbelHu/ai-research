"""Template-requirement validation (anti-hallucination) — design-spec §6D/§7.

Guards that each advisor template's declared response schema (the *template
requirement* shown to the model) stays in lockstep with the strict pydantic
model the wrapper validates into. If they drift, the model could be asked for a
shape we don't enforce — exactly the hallucination gap this requirement closes.
"""

from __future__ import annotations

import pytest
from pydantic import BaseModel

from app.advisor import schemas
from app.advisor.templates import load_template

# (template name, bound pydantic contract)
CASES = [
    ("triage.classify", schemas.Triage),
    ("analyzer.analyze", schemas.Analysis),
    ("analyzer.plan", schemas.PlanSpec),
    ("expert.review", schemas.Verdict),
    ("worker.next_action", schemas.ProposedAction),
    ("junior.answer", schemas.AnswerDraft),
    ("coder.generate", schemas.GeneratedSkill),
]


def _required_fields(model: type[BaseModel]) -> set[str]:
    return {name for name, f in model.model_fields.items() if f.is_required()}


@pytest.mark.parametrize("name,model", CASES)
def test_template_declares_a_requirement(name: str, model: type[BaseModel]) -> None:
    tmpl = load_template(name)
    assert tmpl.schema, f"template {name!r} must declare a response schema requirement"


@pytest.mark.parametrize("name,model", CASES)
def test_template_forbids_extra_fields(name: str, model: type[BaseModel]) -> None:
    # The template requirement and the model both reject invented fields.
    tmpl = load_template(name)
    assert tmpl.schema.get("additionalProperties") is False
    assert model.model_config.get("extra") == "forbid"


@pytest.mark.parametrize("name,model", CASES)
def test_template_required_matches_model(name: str, model: type[BaseModel]) -> None:
    tmpl = load_template(name)
    assert set(tmpl.schema["required"]) == _required_fields(model)
    # every required field is a declared property in the template schema
    assert _required_fields(model) <= set(tmpl.schema["properties"])
