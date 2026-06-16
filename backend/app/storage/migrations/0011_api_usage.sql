-- 0011 api usage budget (web-search credit conservation)
-- A small per-day counter for metered external services (e.g. Tavily web
-- search, whose free credits are limited). The web.search skill increments
-- this on each *real* (non-cached) call and refuses once the daily cap
-- (policies.web_search_daily_max) is reached, so the system can never abuse a
-- limited search quota. One row per (provider, UTC day).

CREATE TABLE api_usage (
    provider   TEXT NOT NULL,
    day        TEXT NOT NULL,                    -- UTC date 'YYYY-MM-DD'
    count      INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (provider, day)
);
