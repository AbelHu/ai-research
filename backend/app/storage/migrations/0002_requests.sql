-- 0002 requests, request details & jobs (design-spec §5, §6C, §9)
-- The user-facing request envelope, appended details, and the unit of work.

CREATE TABLE requests (
    id                  INTEGER PRIMARY KEY,
    -- User-facing handle 'YYYYMMDDHHmmSS[-NN]'; the code IS the folder name.
    code                TEXT NOT NULL UNIQUE,
    session_id          INTEGER REFERENCES sessions(id) ON DELETE SET NULL,
    user_id             INTEGER REFERENCES users(id) ON DELETE SET NULL,
    tenant_id           TEXT,
    workspace           TEXT,
    channel             TEXT,
    title               TEXT,
    status              TEXT,
    -- Links an improvement request back to its origin (§6B).
    improves_request_id INTEGER REFERENCES requests(id) ON DELETE SET NULL,
    importance          REAL,
    use_count           INTEGER NOT NULL DEFAULT 0,
    last_used_at        TEXT,
    expires_at          TEXT,
    state               TEXT NOT NULL DEFAULT 'active'
                        CHECK (state IN ('active', 'archived', 'dropped')),
    created_at          TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Extra info appended after intake; PM appends, Analyzer validates (§6C).
CREATE TABLE request_details (
    id            INTEGER PRIMARY KEY,
    request_id    INTEGER NOT NULL REFERENCES requests(id) ON DELETE CASCADE,
    content       TEXT,
    source        TEXT CHECK (source IN ('user', 'pm')),
    routed_by     TEXT CHECK (routed_by IN ('pm', 'analyzer')),
    confidence    REAL,
    state         TEXT NOT NULL DEFAULT 'active'
                  CHECK (state IN ('active', 'rejected', 'reassigned')),
    reroute_count INTEGER NOT NULL DEFAULT 0,
    created_at    TEXT NOT NULL DEFAULT (datetime('now'))
);

-- One job per request; a complex job's lifecycle is its plan's status (§6B).
CREATE TABLE jobs (
    id          INTEGER PRIMARY KEY,
    request_id  INTEGER NOT NULL REFERENCES requests(id) ON DELETE CASCADE,
    kind        TEXT NOT NULL CHECK (kind IN ('ask', 'task', 'feature')),
    clarity     TEXT,
    complexity  TEXT,
    folder_path TEXT,
    paused      INTEGER NOT NULL DEFAULT 0 CHECK (paused IN (0, 1)),
    paused_at   TEXT,
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX idx_requests_state ON requests(state);
CREATE INDEX idx_requests_improves ON requests(improves_request_id);
CREATE INDEX idx_request_details_request ON request_details(request_id);
CREATE INDEX idx_jobs_request ON jobs(request_id);
