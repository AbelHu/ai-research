"""Tests for the versioned template loader (implementation-plan T3.2)."""

from __future__ import annotations

import pytest

from app.advisor.templates import (
    MissingTemplateVariable,
    TemplateNotFound,
    load_template,
)


def test_render_substitutes_variables_and_pins_version() -> None:
    tmpl = load_template("triage.classify")
    assert tmpl.id == "triage.classify@v1"  # frontmatter version, pinned
    rendered = tmpl.render(text="compare three vendors")
    assert "compare three vendors" in rendered
    assert "{{" not in rendered  # all placeholders consumed


def test_loads_response_schema() -> None:
    tmpl = load_template("triage.classify")
    assert tmpl.schema["properties"]["kind"]["enum"] == ["ask", "task", "feature"]
    assert set(tmpl.schema["required"]) == {
        "kind",
        "clarity",
        "complexity",
        "confidence",
        "rationale",
    }


def test_answer_schema_requires_at_least_one_citation() -> None:
    tmpl = load_template("junior.answer")
    assert tmpl.schema["properties"]["citations"]["minItems"] == 1


def test_missing_variable_raises() -> None:
    tmpl = load_template("triage.classify")
    with pytest.raises(MissingTemplateVariable):
        tmpl.render()  # no `text` provided


def test_unknown_template_raises() -> None:
    with pytest.raises(TemplateNotFound):
        load_template("nope.missing")


def test_custom_templates_dir(tmp_path) -> None:
    (tmp_path / "x.y.md").write_text("---\nversion: 7\n---\nHello {{ name }}", encoding="utf-8")
    tmpl = load_template("x.y", templates_dir=tmp_path)
    assert tmpl.id == "x.y@v7"
    assert tmpl.render(name="world") == "Hello world"
    assert tmpl.schema == {}  # no sibling schema file -> empty
