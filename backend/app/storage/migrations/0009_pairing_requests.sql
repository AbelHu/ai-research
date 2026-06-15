-- 0009 pairing requests (design-spec §10.1; implementation-plan T8.6)
-- User-initiated, host-approved pairing: an unpaired chat account messages the
-- bot, which records a pending request here and replies a short code; the
-- operator approves it on the trusted console (`pair --approve <code>`), which
-- binds the account into `user_identities`.
--
-- The `code` is a **claim ticket**, not an authorization secret: possessing it
-- grants nothing on its own — approval requires console access — so it is stored
-- in plaintext (unlike host `pairing_codes`, where possession proves ownership
-- and only a hash is kept). One pending request per (channel, user); a repeat
-- message reuses the same code (no spam).

CREATE TABLE pairing_requests (
    id              INTEGER PRIMARY KEY,
    channel         TEXT NOT NULL,
    channel_user_id TEXT NOT NULL,
    code            TEXT NOT NULL,
    state           TEXT NOT NULL DEFAULT 'pending'
                    CHECK (state IN ('pending', 'approved', 'expired')),
    expires_at      TEXT NOT NULL,
    approved_at     TEXT,
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (channel, channel_user_id)
);

CREATE INDEX idx_pairing_requests_code ON pairing_requests(code);
CREATE INDEX idx_pairing_requests_state ON pairing_requests(state, expires_at);
