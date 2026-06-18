"""Skills package — deterministic, typed APIs the AI may propose (design-spec §8).

Importing this package triggers ``@skill`` registration for every skill module
(auto-discovery, §8.7): the catalog is populated purely as a side effect of
``import app.skills``. Add new skill modules to the import list below.
"""

from __future__ import annotations

from app.skills import (  # noqa: F401  (imported for @skill side effects)
    codegen,
    data,
    library,
    memory,
    web,
)

# Re-register any user-confirmed ("active") generated skills so a confirmed skill
# survives a process restart; inert bundles stay out until confirmed
# (`codegen.confirm_and_activate`). A broken bundle is skipped (logged), never
# fatal to startup.
codegen.load_active()

__all__ = ["codegen", "data", "library", "memory", "web"]
