-- 0012 plan success criteria (design-spec §6B; autonomy P3 goal-criteria check)
-- Explicit, checkable completion criteria the Analyzer drafts alongside the plan.
-- The per-job runner verifies these before reporting a job completed, so
-- completion is validated against the goal instead of inferred from status
-- transitions alone. Stored as a JSON array of strings (default empty).

ALTER TABLE plans ADD COLUMN success_criteria_json TEXT NOT NULL DEFAULT '[]';
