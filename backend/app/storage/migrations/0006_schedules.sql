-- 0006 schedules & proactive reports (design-spec §9, §11)
-- Interests that drive proactive work, the on-demand scheduler, and run history.

CREATE TABLE user_interests (
    id         INTEGER PRIMARY KEY,
    user_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    topic      TEXT NOT NULL,
    weight     REAL,
    source     TEXT,
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (user_id, topic)
);

-- On-demand scheduler; params_json holds a data product's predefined inputs
-- and generator skills; also hosts the daily 24h TTL-maintenance job.
CREATE TABLE schedules (
    id                 INTEGER PRIMARY KEY,
    kind               TEXT,
    schedule_cron      TEXT,
    params_json        TEXT,
    enabled            INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0, 1)),
    created_by_request INTEGER REFERENCES requests(id) ON DELETE SET NULL,
    last_run_at        TEXT,
    next_run_at        TEXT,
    created_at         TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Proactive digests & data-product runs; one row per run (distinct from
-- final_reports, which are per-job).
CREATE TABLE reports (
    id           INTEGER PRIMARY KEY,
    user_id      INTEGER REFERENCES users(id) ON DELETE SET NULL,
    schedule_id  INTEGER REFERENCES schedules(id) ON DELETE SET NULL,
    title        TEXT,
    summary      TEXT,
    artifact_id  INTEGER REFERENCES artifacts(id) ON DELETE SET NULL,
    delivered_at TEXT,
    created_at   TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX idx_user_interests_user ON user_interests(user_id);
CREATE INDEX idx_schedules_enabled ON schedules(enabled);
CREATE INDEX idx_reports_user ON reports(user_id);
CREATE INDEX idx_reports_schedule ON reports(schedule_id);
