-- 0008 pairing codes (design-spec §10.1; implementation-plan T7.5)
-- Host-minted, single-use codes for the alternative "pair a chat account"
-- path: the operator mints a code on the host (proving ownership), then sends
-- `/pair <code>` from the chat account to bind it to the owner.
--
-- Only a sha256 *hash* of the code is stored (defense in depth): the plaintext
-- is shown once at mint time and never persisted. A code is single-use
-- (`used_at` stamped on consume) and time-boxed (`expires_at`).

CREATE TABLE pairing_codes (
    id         INTEGER PRIMARY KEY,
    code_hash  TEXT NOT NULL UNIQUE,
    expires_at TEXT NOT NULL,
    used_at    TEXT,
    used_by    TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX idx_pairing_codes_active ON pairing_codes(used_at, expires_at);
