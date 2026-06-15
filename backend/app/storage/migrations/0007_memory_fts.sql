-- 0007 memory FTS5 keyword index (design-spec §9; implementation-plan T5.1)
-- An external-content FTS5 mirror of `memories(content, summary)` kept in sync
-- by triggers, so keyword recall stays correct as rows are written/updated/
-- dropped without any change to the repo write paths.
--
-- Dropping a memory nulls its content/summary (the tombstone rule, §9.1); the
-- AFTER UPDATE trigger then removes those terms from the index, so a dropped
-- item leaves the hot FTS index automatically. Archived items keep their
-- content, so `keyword_search` filters on state = 'active' at query time.

CREATE VIRTUAL TABLE memories_fts USING fts5(
    content,
    summary,
    content='memories',
    content_rowid='id',
    tokenize='porter unicode61'
);

-- Keep the FTS index in sync with the base table (external-content pattern).
CREATE TRIGGER memories_fts_ai AFTER INSERT ON memories BEGIN
    INSERT INTO memories_fts(rowid, content, summary)
    VALUES (new.id, new.content, new.summary);
END;

CREATE TRIGGER memories_fts_ad AFTER DELETE ON memories BEGIN
    INSERT INTO memories_fts(memories_fts, rowid, content, summary)
    VALUES ('delete', old.id, old.content, old.summary);
END;

CREATE TRIGGER memories_fts_au AFTER UPDATE ON memories BEGIN
    INSERT INTO memories_fts(memories_fts, rowid, content, summary)
    VALUES ('delete', old.id, old.content, old.summary);
    INSERT INTO memories_fts(rowid, content, summary)
    VALUES (new.id, new.content, new.summary);
END;
