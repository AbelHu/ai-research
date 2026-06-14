"""Deterministic storage layer (design-spec §9).

Pure SQLite, no AI. All later phases persist into this layer; everything is
recoverable from these tables plus the on-disk library folders.
"""
