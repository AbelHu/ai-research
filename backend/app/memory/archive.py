"""Archive compaction (design-spec §9.1/§9.2; implementation-plan T5.9).

When a finished request goes **cold**, its folder is compacted: every file
**except ``final_report.md``** is zipped into ``artifacts.zip`` (process log,
phases, produced files), leaving the final report readable for search/preview.
Compaction is **non-destructive** — the zip holds everything and a deep-search
read **revives** the folder by unzipping it again.
"""

from __future__ import annotations

import os
import zipfile
from pathlib import Path

ARCHIVE_NAME = "artifacts.zip"
# Files kept uncompressed so the report stays readable without unzipping (§9.2).
DEFAULT_KEEP = frozenset({"final_report.md"})


def _files_to_compact(folder: Path, keep: frozenset[str]) -> list[Path]:
    """Every file under ``folder`` except the kept ones and an existing zip."""
    out: list[Path] = []
    for path in sorted(folder.rglob("*")):
        if not path.is_file():
            continue
        if path.name == ARCHIVE_NAME and path.parent == folder:
            continue
        if path.relative_to(folder).as_posix() in keep or path.name in keep:
            continue
        out.append(path)
    return out


def _remove_empty_dirs(folder: Path) -> None:
    """Remove now-empty subdirectories (bottom-up), keeping ``folder`` itself."""
    for dirpath, _dirs, _files in os.walk(folder, topdown=False):
        path = Path(dirpath)
        if path == folder:
            continue
        if not any(path.iterdir()):
            path.rmdir()


def is_compacted(folder: Path) -> bool:
    """Whether ``folder`` has been compacted (has an ``artifacts.zip``)."""
    return (folder / ARCHIVE_NAME).is_file()


def compact_folder(folder: Path, *, keep: frozenset[str] = DEFAULT_KEEP) -> Path:
    """Zip everything except ``keep`` into ``artifacts.zip``; return the zip path.

    The originals are removed after they are safely written into the archive
    (the zip is fully restorable via `revive_folder`). A folder with nothing to
    compact still yields an (empty) archive so the cold state is unambiguous.
    """
    folder = Path(folder)
    zip_path = folder / ARCHIVE_NAME
    targets = _files_to_compact(folder, keep)

    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for path in targets:
            zf.write(path, arcname=path.relative_to(folder).as_posix())

    for path in targets:
        path.unlink()
    _remove_empty_dirs(folder)
    return zip_path


def revive_folder(folder: Path) -> list[str]:
    """Unzip ``artifacts.zip`` back into ``folder`` and remove the zip.

    Returns the restored relative paths (empty if there was nothing to revive).
    A revived folder is indistinguishable from its pre-compaction state.
    """
    folder = Path(folder)
    zip_path = folder / ARCHIVE_NAME
    if not zip_path.is_file():
        return []
    with zipfile.ZipFile(zip_path, "r") as zf:
        names = zf.namelist()
        zf.extractall(folder)
    zip_path.unlink()
    return names
