-- 0001 identity & conversation (design-spec §9)
-- users, channel identities, traits, sessions, messages.

CREATE TABLE users (
    id           INTEGER PRIMARY KEY,
    display_name TEXT,
    github_login TEXT,
    is_owner     INTEGER NOT NULL DEFAULT 0 CHECK (is_owner IN (0, 1)),
    created_at   TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Cross-channel identity mapping + pairing allowlist (only 'paired' may chat).
CREATE TABLE user_identities (
    id              INTEGER PRIMARY KEY,
    user_id         INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    channel         TEXT NOT NULL,
    channel_user_id TEXT NOT NULL,
    state           TEXT NOT NULL DEFAULT 'pending'
                    CHECK (state IN ('pending', 'paired', 'revoked')),
    paired_via      TEXT CHECK (paired_via IN ('device_flow', 'host_code')),
    paired_at       TEXT,
    UNIQUE (channel, channel_user_id)
);

-- The "user characters": habit / liking / location ... (one value per key).
CREATE TABLE user_traits (
    id         INTEGER PRIMARY KEY,
    user_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    key        TEXT NOT NULL,
    value      TEXT,
    source     TEXT,
    confidence REAL,
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (user_id, key)
);

CREATE TABLE sessions (
    id         INTEGER PRIMARY KEY,
    user_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    channel    TEXT,
    status     TEXT,
    started_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE messages (
    id         INTEGER PRIMARY KEY,
    session_id INTEGER NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    direction  TEXT CHECK (direction IN ('in', 'out')),
    content    TEXT,
    raw_json   TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX idx_user_identities_user ON user_identities(user_id);
CREATE INDEX idx_user_traits_user ON user_traits(user_id);
CREATE INDEX idx_sessions_user ON sessions(user_id);
CREATE INDEX idx_messages_session ON messages(session_id);
