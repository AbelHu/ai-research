"""Write + gate generated skills (design-spec §5, §6B; implementation-plan T6.9/T6.10).

A feature job's deliverable is reusable code. It is written **inert** to
``app/skills/generated/<job-code>/`` with a manifest marking it ``inert`` — not
imported, not registered, not executed. Activation is a separate, explicit step
(`activate_generated`) that runs only after Plan Expert review + user
confirmation; it loads each generated module (running its ``@skill`` decorators
so the skills enter the live catalog) and flips the manifest to ``active``.

The generated root is injectable so tests never write into the real package.
"""

from __future__ import annotations

import importlib.util
import json
from dataclasses import dataclass
from pathlib import Path

# Real default location (overridden in tests). Sibling of this module.
GENERATED_ROOT = Path(__file__).resolve().parent / "generated"

MANIFEST_NAME = "manifest.json"
STATUS_INERT = "inert"
STATUS_ACTIVE = "active"


class GeneratedActivationError(RuntimeError):
    """Raised when a generated module can't be loaded/activated."""


@dataclass(frozen=True)
class GeneratedBundle:
    """A job's generated-code folder + its manifest state."""

    job_code: str
    folder: Path
    files: list[str]
    status: str


def _manifest_path(root: Path, job_code: str) -> Path:
    return root / job_code / MANIFEST_NAME


def write_generated_skill(
    root: Path,
    job_code: str,
    filename: str,
    code: str,
) -> Path:
    """Write one inert generated skill file + update the job's manifest.

    Returns the written file path. The code is **not** imported or executed —
    it only lands on disk and is recorded in the manifest as ``inert``.
    """
    if not filename.endswith(".py") or "/" in filename or filename.startswith("."):
        raise ValueError(f"invalid generated filename: {filename!r}")
    folder = root / job_code
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / filename
    path.write_text(code, encoding="utf-8")

    manifest = _load_manifest(root, job_code)
    if filename not in manifest["files"]:
        manifest["files"].append(filename)
    manifest["status"] = STATUS_INERT
    _write_manifest(root, job_code, manifest)
    return path


def _load_manifest(root: Path, job_code: str) -> dict:
    path = _manifest_path(root, job_code)
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    return {"job_code": job_code, "status": STATUS_INERT, "files": []}


def _write_manifest(root: Path, job_code: str, manifest: dict) -> None:
    path = _manifest_path(root, job_code)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8")


def get_bundle(root: Path, job_code: str) -> GeneratedBundle | None:
    """Return a job's generated bundle (folder + manifest), or ``None``."""
    if not _manifest_path(root, job_code).exists():
        return None
    manifest = _load_manifest(root, job_code)
    return GeneratedBundle(
        job_code=job_code,
        folder=root / job_code,
        files=list(manifest.get("files", [])),
        status=manifest.get("status", STATUS_INERT),
    )


def is_inert(root: Path, job_code: str) -> bool:
    """Whether a job's generated code is still inert (not activated)."""
    bundle = get_bundle(root, job_code)
    return bundle is not None and bundle.status == STATUS_INERT


def activate_generated(root: Path, job_code: str) -> list[str]:
    """Load + register a job's generated skills; flip the manifest to ``active``.

    Imports each generated module by path so its ``@skill`` decorators run
    against the live registry. Returns the names newly present in the catalog
    after activation. Call **only** after review + user confirmation (§6B).
    """
    from app.skills.registry import REGISTRY

    bundle = get_bundle(root, job_code)
    if bundle is None:
        raise GeneratedActivationError(f"no generated bundle for job {job_code!r}")

    before = set(REGISTRY)
    for filename in bundle.files:
        path = bundle.folder / filename
        module_name = f"app.skills.generated.{job_code}.{Path(filename).stem}"
        spec = importlib.util.spec_from_file_location(module_name, path)
        if spec is None or spec.loader is None:  # pragma: no cover - defensive
            raise GeneratedActivationError(f"cannot load generated module: {path}")
        module = importlib.util.module_from_spec(spec)
        try:
            spec.loader.exec_module(module)
        except Exception as exc:  # noqa: BLE001 - surface any generated-code error
            raise GeneratedActivationError(f"failed to activate {path}: {exc}") from exc

    manifest = _load_manifest(root, job_code)
    manifest["status"] = STATUS_ACTIVE
    _write_manifest(root, job_code, manifest)
    return sorted(set(REGISTRY) - before)


def confirm_and_activate(root: Path, job_code: str, *, confirmed: bool) -> list[str]:
    """Gate activation on user confirmation (design-spec §5/§6B; plan T6.10).

    With ``confirmed=True`` (Plan Expert reviewed + user confirmed) the generated
    skills are loaded + registered and the manifest flips to ``active``. With
    ``confirmed=False`` (decline) **nothing is loaded** — the code stays inert.
    Returns the newly-activated skill names (empty on decline).
    """
    if not confirmed:
        return []
    return activate_generated(root, job_code)
