"""On-disk folder library + index files (design-spec §9.2; implementation-plan T5.7).

The library is the durable, outside-SQLite record of work: one folder per request
(named by its `code`, the `/req <id>` handle), grouped by kind, plus two index
files mapping keywords/tags/brief-description → folder path.

```
data/library/
  Active/{Simple,Tasks,Features}/<request-code>/
  Archive/<yyyy>/<request-code>/
  index.json            # active/archived entries (the hot, searchable map)
  index.dropped.json    # dropped entries (full/deep search + improvement only)
```

This module is pure filesystem + JSON — no DB, no model. The Librarian (T5.8)
calls it as the single writer; the DB `library_index` mirror is the hot subset.
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import asdict, dataclass, field
from pathlib import Path

# Job kind → its Active/ subdirectory (§9.2).
KIND_DIRS = {"ask": "Simple", "task": "Tasks", "feature": "Features"}

INDEX_NAME = "index.json"
DROPPED_INDEX_NAME = "index.dropped.json"


@dataclass(frozen=True)
class IndexEntry:
    """One library index record (mirrors the DB `library_index` columns)."""

    request_code: str
    folder_path: str
    object_type: str = "request"
    keywords: list[str] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)
    brief_description: str = ""


def library_root(data_dir: Path) -> Path:
    """Return the library root (``<data_dir>/library``)."""
    return Path(data_dir) / "library"


def active_dir(root: Path, kind: str) -> Path:
    """The Active/ subdirectory for a job kind (ask→Simple, task→Tasks, …)."""
    try:
        sub = KIND_DIRS[kind]
    except KeyError:
        raise ValueError(f"unknown job kind: {kind!r}") from None
    return root / "Active" / sub


def request_folder(root: Path, kind: str, request_code: str) -> Path:
    """The per-request working folder under Active/."""
    return active_dir(root, kind) / request_code


def ensure_request_folder(root: Path, kind: str, request_code: str) -> Path:
    """Create (if needed) and return a request's Active/ folder."""
    folder = request_folder(root, kind, request_code)
    folder.mkdir(parents=True, exist_ok=True)
    return folder


def _atomic_write_json(path: Path, data: object) -> None:
    """Write JSON atomically (temp file + replace) so a crash can't truncate it."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2, ensure_ascii=False, sort_keys=True)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def _load_json_map(path: Path) -> dict[str, dict]:
    if not path.exists():
        return {}
    with open(path, encoding="utf-8") as fh:
        data = json.load(fh)
    return data if isinstance(data, dict) else {}


def load_index(root: Path, *, dropped: bool = False) -> dict[str, dict]:
    """Load the index map (``index.json`` or ``index.dropped.json``)."""
    name = DROPPED_INDEX_NAME if dropped else INDEX_NAME
    return _load_json_map(root / name)


def upsert_index_entry(root: Path, entry: IndexEntry) -> IndexEntry:
    """Insert/update an entry in ``index.json`` (keyed by request code)."""
    index = load_index(root)
    index[entry.request_code] = asdict(entry)
    _atomic_write_json(root / INDEX_NAME, index)
    return entry


def get_index_entry(root: Path, request_code: str, *, dropped: bool = False) -> dict | None:
    """Return an index entry by request code (or ``None``)."""
    return load_index(root, dropped=dropped).get(request_code)


def move_to_dropped(root: Path, request_code: str) -> bool:
    """Move an entry from ``index.json`` to ``index.dropped.json`` (§9.1).

    Returns ``True`` if an entry was moved, ``False`` if none existed. The
    on-disk folder is left in place — only the index membership changes.
    """
    index = load_index(root)
    entry = index.pop(request_code, None)
    if entry is None:
        return False
    dropped = load_index(root, dropped=True)
    dropped[request_code] = entry
    _atomic_write_json(root / INDEX_NAME, index)
    _atomic_write_json(root / DROPPED_INDEX_NAME, dropped)
    return True
