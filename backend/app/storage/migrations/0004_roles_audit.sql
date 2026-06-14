-- 0004 roles & audit (design-spec §6A, §6D, §7, §9)
-- Agent registry, the inter-role envelope queue/log, AI-call audit, audit log.

CREATE TABLE agents (
    id             INTEGER PRIMARY KEY,
    job_id         INTEGER REFERENCES jobs(id) ON DELETE CASCADE,
    role           TEXT NOT NULL,
    scope          TEXT NOT NULL CHECK (scope IN ('company', 'job')),
    status         TEXT,
    pid_or_thread  TEXT,
    last_active_at TEXT
);

-- The inter-role envelope queue + durable log; Boss routes on `action` (§6D).
CREATE TABLE role_messages (
    id           INTEGER PRIMARY KEY,
    request_id   INTEGER NOT NULL REFERENCES requests(id) ON DELETE CASCADE,
    job_id       INTEGER REFERENCES jobs(id) ON DELETE CASCADE,
    from_role    TEXT NOT NULL,
    to_role      TEXT NOT NULL,
    action       TEXT NOT NULL,
    payload_json TEXT,
    template     TEXT,
    status       TEXT NOT NULL DEFAULT 'queued'
                 CHECK (status IN ('queued', 'in_progress', 'done', 'failed')),
    -- Trace chain: the envelope that caused this one.
    causation_id INTEGER REFERENCES role_messages(id) ON DELETE SET NULL,
    created_at   TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Every model call is recorded, keyed by request_id (even pre-job calls) (§7).
CREATE TABLE ai_calls (
    id                INTEGER PRIMARY KEY,
    request_id        INTEGER NOT NULL REFERENCES requests(id) ON DELETE CASCADE,
    role_message_id   INTEGER REFERENCES role_messages(id) ON DELETE SET NULL,
    job_id            INTEGER REFERENCES jobs(id) ON DELETE CASCADE,
    step_id           INTEGER REFERENCES steps(id) ON DELETE SET NULL,
    role              TEXT,
    model_id          TEXT,
    template          TEXT,
    prompt_ref        TEXT,
    response_ref      TEXT,
    tokens            INTEGER,
    latency_ms        INTEGER,
    validation_status TEXT,
    created_at        TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE audit_log (
    id           INTEGER PRIMARY KEY,
    actor        TEXT CHECK (actor IN ('system', 'ai', 'user', 'role')),
    action       TEXT,
    target       TEXT,
    payload_json TEXT,
    created_at   TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX idx_agents_job ON agents(job_id);
CREATE INDEX idx_role_messages_request ON role_messages(request_id);
CREATE INDEX idx_role_messages_causation ON role_messages(causation_id);
CREATE INDEX idx_role_messages_status ON role_messages(status);
CREATE INDEX idx_ai_calls_request ON ai_calls(request_id);
