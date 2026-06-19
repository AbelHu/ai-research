-- 0014 library compaction: when was a library entry last accessed (read/revived)?
--
-- The cold-folder compaction (app.memory.archive.compact_folder, run by the
-- Librarian on a schedule) zips a closed request's artifacts after a quiet
-- period to save storage, keeping only the final report readable. Tracking
-- last_used_at lets it SKIP entries still being accessed via memory and reset
-- the clock when a folder is revived back to hot.
ALTER TABLE library_index ADD COLUMN last_used_at TEXT;
