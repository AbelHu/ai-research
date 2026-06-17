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
UnpairedReply = Literal["pair_hint", "silent"]


class MemoryPolicy(BaseModel):
    """Deterministic memory weighting / TTL knobs (design-spec §9.1).

    These tune the effective-weight formula and the retention clock; the AI
    never sets them. ``core`` has no decay and no TTL, so it has no knobs here.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    # Reinforcement coefficient β in (1 + β·ln(1 + use_count)).
    reinforce_beta: float = Field(default=0.3, ge=0)
    # Bounded importance bump applied on each reinforcing read (capped at 1.0).
    reinforce_importance_step: float = Field(default=0.02, ge=0)
    # Max extra TTL (days) granted on a reinforcing read, scaled by importance.
    importance_ttl_scale_days: float = Field(default=30.0, ge=0)
    # Base TTL (days) per retention class on write; scaled by (1 + importance).
    base_ttl_ephemeral_days: float = Field(default=1.0, ge=0)
    base_ttl_short_days: float = Field(default=14.0, ge=0)
    base_ttl_long_days: float = Field(default=180.0, ge=0)
    # Recency-decay rate λ (per day) per class; larger = forgets faster.
    decay_lambda_ephemeral: float = Field(default=1.0, ge=0)
    decay_lambda_short: float = Field(default=0.1, ge=0)
    decay_lambda_long: float = Field(default=0.02, ge=0)
    # A 'long' item whose effective weight falls below this archives on sweep.
    archive_threshold: float = Field(default=0.05, ge=0)
    # On expiry: importance at/below this may be dropped; above it → archived.
    drop_importance_max: float = Field(default=0.5, ge=0, le=1)
    # A 'short' item used at least this many times is promoted to 'long'.
    promote_use_count: int = Field(default=5, ge=1)


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
    # Retry attempts for transient background-job failures.
    max_job_retries: int = Field(default=1, ge=0)
    # Max skill proposals a task may execute before the Senior Worker stops.
    max_task_steps: int = Field(default=1, ge=1)
    junior_session_idle_minutes: int = Field(default=15, ge=1)
    progress_updates: ProgressUpdates = "phase"
    # Verify that any URL cited in an answer actually exists before the answer
    # is accepted (anti-hallucination, §7.1). Default on. May be disabled where
    # our deterministic fetch is blocked by anti-crawler defenses (CAPTCHA,
    # JS/bot challenges, paywalls) that an AI/browser could pass — a first cut to
    # be hardened later, not a permanent limitation.
    verify_citation_urls: bool = True
    # Require user confirmation before activating a feature job's generated
    # code/skills (design-spec §5/§6B). Default on: generated code stays inert
    # until explicitly confirmed; deactivating this would auto-activate (unsafe).
    confirm_generated_code: bool = True
    # Gateway allowlist (§10.1). A chat bot is publicly reachable, so the Gateway
    # refuses every unpaired sender. `unpaired_reply` chooses whether to send a
    # single "pair first" hint or stay silent; refusals are rate-limited per
    # sender to resist probing/flooding (cap N actioned refusals per window —
    # beyond that they're dropped without a reply or a new audit row).
    unpaired_reply: UnpairedReply = "pair_hint"
    refusal_rate_limit_max: int = Field(default=3, ge=1)
    refusal_rate_limit_window_seconds: int = Field(default=60, ge=1)
    # Web search (Tavily) is **metered** — credits are limited. Conserve them:
    # cap real (non-cached) searches per UTC day, and cache identical queries so
    # repeats cost nothing. 0 disables the cap / cache respectively. Free tier is
    # ~1,000/mo (≈33/day), so 50/day is generous but bounded.
    web_search_daily_max: int = Field(default=50, ge=0)
    web_search_cache_ttl_minutes: int = Field(default=15, ge=0)
    # Deterministic memory weighting / TTL knobs (§9.1).
    memory: MemoryPolicy = Field(default_factory=MemoryPolicy)


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
