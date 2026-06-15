"""Skills package — deterministic, typed APIs the AI may propose (design-spec §8).

Importing this package triggers ``@skill`` registration for every skill module
(auto-discovery, §8.7): the catalog is populated purely as a side effect of
``import app.skills``. Add new skill modules to the import list below.
"""

from __future__ import annotations

from app.skills import library, memory  # noqa: F401  (imported for @skill side effects)

__all__ = ["library", "memory"]
