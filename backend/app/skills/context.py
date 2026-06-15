"""`SkillContext` — the deterministic services a skill function receives (§8.5).

A skill is handed a context of **deterministic services only**: the DB
connection, the granted permission set, the current user/job/task ids, config
and a logger. The AI/model is deliberately **absent** — skills are
model-independent code and can never call a model.
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass, field

from app.config.settings import Settings

# Module logger used when a context is built without an explicit one.
_DEFAULT_LOGGER = logging.getLogger("app.skills")


@dataclass
class SkillContext:
    """Deterministic services passed to every skill function (§8.5).

    There is intentionally **no** model/provider/advisor here: a skill cannot
    reach the AI. The `permissions` set is what the running user/role has been
    granted; the policy gate (§8.6) checks a skill's required permissions
    against it.
    """

    user_id: int
    conn: sqlite3.Connection
    permissions: frozenset[str] = frozenset()
    job_id: int | None = None
    task_id: int | None = None  # plan_task_id — groups recorded steps (§8.6)
    config: Settings | None = None
    logger: logging.Logger = field(default_factory=lambda: _DEFAULT_LOGGER)
