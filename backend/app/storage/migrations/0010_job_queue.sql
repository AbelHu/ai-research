-- 0010 job execution queue (design-spec §6B; service slice B2)
-- A planned job (a complex task/feature, or a simple ask escalated by the
-- control loop) that the background **job worker** must run end-to-end — Senior
-- Worker executes the plan's tasks, the Plan Expert resolves phases, the Company
-- Expert signs off — and then deliver the result back to the originating chat.
-- One row per job (PRIMARY KEY on job_id makes enqueue idempotent).
--
-- We only have a single chat window today, so delivery is addressed by the
-- originating (channel, chat_id) and quotes the user's original message
-- (reply_to_message_id) so the follow-up reply is clearly tied to its /req.

CREATE TABLE job_queue (
    job_id              INTEGER PRIMARY KEY REFERENCES jobs(id) ON DELETE CASCADE,
    status              TEXT NOT NULL DEFAULT 'pending'
                        CHECK (status IN ('pending', 'running', 'done', 'failed')),
    channel             TEXT,
    chat_id             TEXT,
    reply_to_message_id TEXT,
    user_id             INTEGER REFERENCES users(id) ON DELETE SET NULL,
    result              TEXT,
    error               TEXT,
    attempts            INTEGER NOT NULL DEFAULT 0,
    created_at          TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at          TEXT
);

-- The worker claims the oldest pending job; this index serves that scan.
CREATE INDEX idx_job_queue_status ON job_queue(status, job_id);
