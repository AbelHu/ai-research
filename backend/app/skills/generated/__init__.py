"""Generated skills — **inert by default** (design-spec §5, §6B; plan T6.9/T6.10).

A **feature** (or improvement) job writes proposed skills/code into
``app/skills/generated/<job-code>/``. This package is **deliberately NOT
auto-imported** by ``app.skills`` — generated code stays **inert** (never
registered in the catalog, never executed) until it is explicitly reviewed and
**user-confirmed**, at which point `app.skills.codegen.activate_generated` loads
it and runs its ``@skill`` decorators (§6B gating).

Once confirmed, a bundle's manifest is flipped to ``active``; on every process
start `app.skills.codegen.load_active` re-imports the **active** bundles (called
from ``app.skills.__init__``) so a confirmed skill survives a restart. Inert
bundles remain excluded until confirmed.
"""
