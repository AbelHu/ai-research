-- 0005 memory & library (design-spec §9, §9.1, §9.2)
-- Weighted/TTL memories with tombstone-on-drop, tags, cold archive, final
-- reports, the library index mirror, embeddings, and artifacts.

CREATE TABLE memories (
    id              INTEGER PRIMARY KEY,
    user_id         INTEGER REFERENCES users(id) ON DELETE SET NULL,
    tenant_id       TEXT,
    workspace       TEXT,
    kind            TEXT,
    -- Normalized key for evolving facts (e.g. 'location:home') -> supersede.
    entity_key      TEXT,
    content         TEXT,
    summary         TEXT,
    importance      REAL,
    retention_class TEXT
                    CHECK (retention_class IN ('ephemeral', 'short', 'long', 'core')),
    confidence      REAL,
    decay_rate      REAL,
    use_count       INTEGER NOT NULL DEFAULT 0,
    last_used_at    TEXT,
    expires_at      TEXT,
    version         INTEGER NOT NULL DEFAULT 1,
    superseded_by   INTEGER REFERENCES memories(id) ON DELETE SET NULL,
    -- dropped = thin tombstone (content offloaded to disk) so chains survive.
    state           TEXT NOT NULL DEFAULT 'active'
                    CHECK (state IN ('active', 'archived', 'dropped')),
    source_ref      TEXT,
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at      TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE memory_tags (
    memory_id INTEGER NOT NULL REFERENCES memories(id) ON DELETE CASCADE,
    tag       TEXT NOT NULL,
    PRIMARY KEY (memory_id, tag)
);

-- Cold store: artifacts zipped (non-destructive), excluded from the hot index.
CREATE TABLE memory_archive (
    memory_id          INTEGER PRIMARY KEY REFERENCES memories(id) ON DELETE CASCADE,
    compressed_content BLOB,
    archived_at        TEXT NOT NULL DEFAULT (datetime('now'))
);

-- One per job; the durable card sent to the user for confirmation (§9.2).
CREATE TABLE final_reports (
    id                           INTEGER PRIMARY KEY,
    request_id                   INTEGER NOT NULL REFERENCES requests(id) ON DELETE CASCADE,
    job_id                       INTEGER REFERENCES jobs(id) ON DELETE SET NULL,
    keywords_json                TEXT,
    tags_json                    TEXT,
    brief_description            TEXT,
    gain_good                    TEXT,
    gain_bad                     TEXT,
    gain_improve                 TEXT,
    improvement_suggestions_json TEXT,
    user_confirmed               INTEGER NOT NULL DEFAULT 0
                                 CHECK (user_confirmed IN (0, 1)),
    spawned_request_id           INTEGER REFERENCES requests(id) ON DELETE SET NULL,
    outcome                      TEXT,
    artifact_path                TEXT,
    created_at                   TEXT NOT NULL DEFAULT (datetime('now'))
);

-- DB mirror of the on-disk index file; holds active/archived entries only -
-- a dropped item's row is deleted here (kept on disk in index.dropped) (§9.1).
CREATE TABLE library_index (
    id                INTEGER PRIMARY KEY,
    request_id        INTEGER NOT NULL REFERENCES requests(id) ON DELETE CASCADE,
    object_type       TEXT,
    keywords_json     TEXT,
    tags_json         TEXT,
    brief_description TEXT,
    folder_path       TEXT,
    db_refs_json      TEXT,
    created_at        TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Hot vector index; (object_type, object_id) addresses any embeddable row.
CREATE TABLE embeddings (
    object_type TEXT NOT NULL,
    object_id   INTEGER NOT NULL,
    vector      BLOB,
    PRIMARY KEY (object_type, object_id)
);

CREATE TABLE artifacts (
    id         INTEGER PRIMARY KEY,
    job_id     INTEGER NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    path       TEXT,
    mime       TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX idx_memories_user ON memories(user_id);
CREATE INDEX idx_memories_entity_key ON memories(entity_key);
CREATE INDEX idx_memories_state ON memories(state);
CREATE INDEX idx_memories_superseded ON memories(superseded_by);
CREATE INDEX idx_memory_tags_tag ON memory_tags(tag);
CREATE INDEX idx_final_reports_request ON final_reports(request_id);
CREATE INDEX idx_library_index_request ON library_index(request_id);
CREATE INDEX idx_artifacts_job ON artifacts(job_id);
