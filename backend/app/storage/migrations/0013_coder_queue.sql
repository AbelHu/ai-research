-- 0013 coder queue (dedicated codegen lane; P4)
-- A feature job hands skill generation off to a separate, privileged **coder
-- worker** (fs-write + subprocess sandbox). This queue is the decoupling seam:
-- the main pipeline enqueues one *coding request* per feature job; the coder
-- worker consumes it, produces a validated **inert** bundle, and records the
-- result here. The contract is transport-agnostic so the coding subsystem can
-- later move to its own service without changing callers.

CREATE TABLE coder_queue (
    job_id              INTEGER PRIMARY KEY REFERENCES jobs(id) ON DELETE CASCADE,
    request_id          INTEGER NOT NULL REFERENCES requests(id) ON DELETE CASCADE,
    job_code            TEXT NOT NULL,
    goal                TEXT NOT NULL,
    status              TEXT NOT NULL DEFAULT 'pending'
                        CHECK (status IN ('pending', 'running', 'done', 'failed')),
    channel             TEXT,
    chat_id             TEXT,
    reply_to_message_id TEXT,
    user_id             INTEGER REFERENCES users(id) ON DELETE SET NULL,
    skill_modules       TEXT,   -- JSON list of promoted skill-module filenames
    validation          TEXT,   -- JSON validation summary (checks + iterations)
    error               TEXT,
    attempts            INTEGER NOT NULL DEFAULT 0,
    created_at          TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at          TEXT
);

-- The coder worker claims the oldest pending coding request; this index serves
-- that scan.
CREATE INDEX idx_coder_queue_status ON coder_queue(status, job_id);
