"""Tests for the deterministic tool-policy gate (design-spec §8.6).

Offline + pure: assert that a request's advised ``domain`` maps to the right
allowed skill set — coding work drops external web research; other domains keep
the full set. The AI only advises the domain; this module owns the decision.
"""

from __future__ import annotations

import app.skills  # noqa: F401 -- ensure @skill registration populates REGISTRY
from app.skills import toolpolicy


def test_coding_domain_excludes_external_research() -> None:
    allowed = toolpolicy.allowed_skills(domain="coding")
    assert "web.search" not in allowed
    assert "web.fetch" not in allowed
    # Local tools remain available to a coding task.
    assert "memory.search" in allowed


def test_research_domain_keeps_web_tools() -> None:
    allowed = toolpolicy.allowed_skills(domain="research")
    assert "web.search" in allowed
    assert "web.fetch" in allowed


def test_general_domain_keeps_web_tools() -> None:
    allowed = toolpolicy.allowed_skills(domain="general")
    assert "web.search" in allowed


def test_unknown_domain_fails_open_to_full_set() -> None:
    # A new/unmapped label must not silently strip tools — it defaults to all.
    allowed = toolpolicy.allowed_skills(domain="banana")
    assert "web.search" in allowed


def test_base_set_is_filtered_not_expanded_for_coding() -> None:
    base = {"memory.search", "web.search"}
    allowed = toolpolicy.allowed_skills(domain="coding", base=base)
    assert allowed == {"memory.search"}


def test_base_set_preserved_for_general() -> None:
    base = {"memory.search", "web.search"}
    allowed = toolpolicy.allowed_skills(domain="general", base=base)
    assert allowed == base
