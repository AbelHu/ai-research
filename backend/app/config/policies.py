"""Policy knobs loader (design-spec §6A/§6B/§6C, implementation-plan T0.5).

`config/policies.yaml` is the single source of truth for tunable limits. This
module loads and validates it into a typed, frozen `Policies` object so the
rest of the codebase reads strongly-typed values instead of dict lookups.

Deterministic code reads these knobs; the AI never sets them.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field

# Repo root = backend/app/config/policies.py -> parents[3]
REPO_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_POLICIES_CONFIG = REPO_ROOT / "config" / "policies.yaml"

ProgressUpdates = Literal["none", "phase", "task"]


class Policies(BaseModel):
    """Typed policy knobs. Field defaults are the documented platform defaults.

    Unknown keys in the YAML are rejected (`extra="forbid"`) so a typo in
    `policies.yaml` fails loudly instead of being silently ignored.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    max_phase_declines: int = Field(default=3, ge=0)
    max_improvement_iterations: int = Field(default=2, ge=0)
    max_append_reroutes: int = Field(default=1, ge=0)
    max_concurrent_jobs: int = Field(default=3, ge=1)
    junior_session_idle_minutes: int = Field(default=15, ge=1)
    progress_updates: ProgressUpdates = "phase"
    # Verify that any URL cited in an answer actually exists before the answer
    # is accepted (anti-hallucination, §7.1). Default on. May be disabled where
    # our deterministic fetch is blocked by anti-crawler defenses (CAPTCHA,
    # JS/bot challenges, paywalls) that an AI/browser could pass — a first cut to
    # be hardened later, not a permanent limitation.
    verify_citation_urls: bool = True


def load_policies(path: Path | None = None) -> Policies:
    """Load and validate policy knobs from YAML.

    A missing file or empty document yields the documented defaults; any keys
    present override those defaults.
    """
    cfg_path = path or DEFAULT_POLICIES_CONFIG
    if not cfg_path.exists():
        return Policies()
    with open(cfg_path, encoding="utf-8") as fh:
        raw = yaml.safe_load(fh)
    return Policies.model_validate(raw or {})


@lru_cache(maxsize=1)
def get_policies() -> Policies:
    return load_policies()
