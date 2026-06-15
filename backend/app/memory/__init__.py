"""Memory & library services (design-spec §9, §9.1, §9.2).

This package holds the deterministic retrieval + lifecycle layer that sits on
top of the P1 `memories` repo: FTS keyword search, the pure-Python vector store,
hybrid ranking, the effective-weight / TTL math, the daily sweep, and the
on-disk folder library + final-report commit.
"""
