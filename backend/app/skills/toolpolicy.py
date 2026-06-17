"""Deterministic tool-policy gate: which skills a request's domain may use (§8.6).

The Analyzer **advises** a request's ``domain`` (coding / research / general)
during classification; this module maps that advice to the **allowed skill set**
a worker may propose from. The AI only advises the domain — deterministic code
owns the allow/deny decision, keeping the model out of the control path (§6C).

Why gate by domain:

* ``coding`` work is self-contained against the user's project/local context, so
  it **excludes external web research tools** (``web.search`` / ``web.fetch``).
  That conserves metered search credits and latency without hurting code quality.
* ``research`` / ``general`` work keeps the full tool set (web research included).

The same pattern generalizes to other tool groups: add a group below and a rule
in :func:`allowed_skills`.
"""

from __future__ import annotations

from app.skills.registry import REGISTRY

# Skills that reach the public web for research. A coding-domain request works
# from local context, so these are removed from its allowed set.
EXTERNAL_RESEARCH_SKILLS = frozenset({"web.search", "web.fetch"})

# Domains the Analyzer may assign; "general" is the safe default (all tools).
CODING = "coding"
RESEARCH = "research"
GENERAL = "general"


def allowed_skills(*, domain: str, base: set[str] | None = None) -> set[str]:
    """Return the skill names a request of ``domain`` is allowed to use.

    Args:
        domain: the Analyzer's advised work domain (``coding`` / ``research`` /
            ``general``). An unknown value is treated as ``general`` (no gating)
            so a new/unmapped label fails open to the safe default.
        base: the candidate skill names to filter (defaults to every registered
            skill). Pass a role's already-restricted set to intersect with it.

    Coding work drops the external web-research tools; other domains keep
    ``base`` unchanged.
    """
    names = set(base) if base is not None else set(REGISTRY)
    if domain == CODING:
        return names - EXTERNAL_RESEARCH_SKILLS
    return names
