-- 0003 plans, phases, tasks & steps (design-spec §6B, §8.6, §9)
-- The complex-job hierarchy (plan -> phases -> recursive tasks) + the process.

CREATE TABLE plans (
    id          INTEGER PRIMARY KEY,
    job_id      INTEGER NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    status      TEXT NOT NULL DEFAULT 'New'
                CHECK (status IN ('New', 'Approved', 'InProgress',
                                  'Resolved', 'Closed', 'Abandoned')),
    approved_by TEXT,
    resolved_by TEXT,
    closed_by   TEXT,
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE phases (
    id            INTEGER PRIMARY KEY,
    plan_id       INTEGER NOT NULL REFERENCES plans(id) ON DELETE CASCADE,
    idx           INTEGER NOT NULL,
    title         TEXT,
    status        TEXT NOT NULL DEFAULT 'New'
                  CHECK (status IN ('New', 'Approved', 'Active', 'InProgress',
                                    'Resolved', 'Closed', 'Abandoned')),
    decline_count INTEGER NOT NULL DEFAULT 0,
    report_ref    TEXT,
    resolved_by   TEXT,
    signed_off_by TEXT,
    created_at    TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Recursive: a task may own subtasks via parent_task_id.
CREATE TABLE plan_tasks (
    id              INTEGER PRIMARY KEY,
    phase_id        INTEGER NOT NULL REFERENCES phases(id) ON DELETE CASCADE,
    parent_task_id  INTEGER REFERENCES plan_tasks(id) ON DELETE CASCADE,
    title           TEXT,
    status          TEXT NOT NULL DEFAULT 'New'
                    CHECK (status IN ('New', 'Approved', 'InProgress',
                                      'Resolved', 'Closed', 'Abandoned')),
    run_mode        TEXT NOT NULL DEFAULT 'serial'
                    CHECK (run_mode IN ('serial', 'parallel')),
    depends_on_json TEXT,
    owner_role      TEXT,
    created_at      TEXT NOT NULL DEFAULT (datetime('now'))
);

-- The recorded "process": one row per skill invocation (§8.6).
CREATE TABLE steps (
    id              INTEGER PRIMARY KEY,
    job_id          INTEGER NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    plan_task_id    INTEGER REFERENCES plan_tasks(id) ON DELETE CASCADE,
    idx             INTEGER NOT NULL,
    skill_name      TEXT,
    params_json     TEXT,
    status          TEXT,
    result_json     TEXT,
    provenance_json TEXT,
    started_at      TEXT,
    ended_at        TEXT
);

CREATE INDEX idx_plans_job ON plans(job_id);
CREATE INDEX idx_phases_plan ON phases(plan_id);
CREATE INDEX idx_plan_tasks_phase ON plan_tasks(phase_id);
CREATE INDEX idx_plan_tasks_parent ON plan_tasks(parent_task_id);
CREATE INDEX idx_steps_job ON steps(job_id);
CREATE INDEX idx_steps_task ON steps(plan_task_id);
