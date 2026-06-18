"""Sandboxed validation of generated skill bundles (Coder subsystem; P1).

Untrusted, model-generated skill code must never be imported or executed in the
worker's own interpreter. This module runs the validation checks — **import**,
**lint** (ruff), and **tests** (pytest) — in an isolated **subprocess**, behind
a pluggable :class:`Sandbox` backend:

* :class:`BubblewrapSandbox` — preferred when ``bwrap`` is installed: Linux
  user-namespace isolation with **no network** (``--unshare-all``), a read-only
  view of the interpreter/repo, and a writable bind only for the bundle dir.
* :class:`RlimitSandbox` — the always-available fallback: a plain subprocess with
  ``resource`` limits (CPU, file size, no core dumps), a hard wall-clock timeout,
  and a scrubbed environment.

:func:`run_checks` orchestrates the three checks over a bundle directory and
returns a :class:`ValidationReport`. The `Sandbox` is injectable so the
orchestration is unit-testable with the fast fallback (no ``bwrap`` required),
and the bubblewrap argv construction is a pure function that's tested directly.

> Isolation note: the fallback caps CPU/wall/file-size but **not** memory
> (address-space rlimits are flaky); true memory caps + stronger isolation come
> from ``bwrap`` (and, later, cgroups). Generated code is additionally constrained
> to ``effect="read"`` and stays inert until user confirmation, so this is
> defense-in-depth, not the only guard.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Protocol

try:  # POSIX only; absent on Windows.
    import resource
except ImportError:  # pragma: no cover - non-POSIX
    resource = None  # type: ignore[assignment]

logger = logging.getLogger("app.coder.sandbox")

# Cap on captured output per check (enough to show the error, short enough for
# logs / the repair prompt).
_OUTPUT_LIMIT = 6_000
# Default per-check wall-clock timeout (seconds).
DEFAULT_TIMEOUT = 30
# File-size cap for anything the validated code writes (defense vs disk-fill).
_FSIZE_BYTES = 50 * 1024 * 1024

# Backend root = ``backend/`` (parent of the ``app`` package), so a generated
# module's ``from app.skills.registry import skill`` resolves in the subprocess.
_BACKEND_ROOT = Path(__file__).resolve().parents[2]

# Import harness: imports every non-test ``*.py`` in the bundle dir by path so
# syntax/import errors surface and each ``@skill`` decorator runs. Exit non-zero
# if any module fails to import.
_IMPORT_HARNESS = r"""
import importlib.util, pathlib, sys, traceback

bundle = pathlib.Path(sys.argv[1])
failed = False
for path in sorted(bundle.glob("*.py")):
    if path.name.startswith("test_") or path.name == "__init__.py":
        continue
    name = "genmod_" + path.stem
    try:
        spec = importlib.util.spec_from_file_location(name, path)
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
        print("OK", path.name)
    except Exception:
        failed = True
        print("FAIL", path.name)
        traceback.print_exc()
sys.exit(1 if failed else 0)
"""


@dataclass(frozen=True)
class RunResult:
    """The raw result of one sandboxed subprocess run."""

    returncode: int
    stdout: str
    stderr: str
    timed_out: bool


@dataclass(frozen=True)
class CheckResult:
    """One validation check's outcome (import / lint / tests)."""

    name: str
    ok: bool
    skipped: bool
    timed_out: bool
    output: str
    duration_ms: int


@dataclass(frozen=True)
class ValidationReport:
    """The combined result of running all checks over a bundle."""

    ok: bool
    checks: tuple[CheckResult, ...]

    def by_name(self, name: str) -> CheckResult | None:
        return next((c for c in self.checks if c.name == name), None)

    @property
    def summary(self) -> str:
        return " ".join(
            f"{c.name}={'skip' if c.skipped else ('ok' if c.ok else 'FAIL')}" for c in self.checks
        )


def _truncate(text: str) -> str:
    text = text.strip()
    return text if len(text) <= _OUTPUT_LIMIT else text[:_OUTPUT_LIMIT] + "\n…(truncated)"


def _apply_rlimits() -> None:  # pragma: no cover - runs in the child process
    """Cap CPU seconds, file size, and core dumps in the child (preexec)."""
    if resource is None:
        return
    cpu = max(2, DEFAULT_TIMEOUT)
    resource.setrlimit(resource.RLIMIT_CPU, (cpu, cpu))
    resource.setrlimit(resource.RLIMIT_FSIZE, (_FSIZE_BYTES, _FSIZE_BYTES))
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))


def _spawn(argv: list[str], *, cwd: Path, env: dict[str, str], timeout: int) -> RunResult:
    """Run ``argv`` to completion (or timeout), capturing output."""
    preexec = _apply_rlimits if (resource is not None and sys.platform != "win32") else None
    try:
        proc = subprocess.run(
            argv,
            cwd=str(cwd),
            env=env,
            capture_output=True,
            text=True,
            timeout=timeout,
            start_new_session=True,
            preexec_fn=preexec,  # noqa: PLW1509 - intentional child rlimits
        )
    except subprocess.TimeoutExpired as exc:
        return RunResult(
            returncode=-1,
            stdout=str(exc.stdout or ""),
            stderr=str(exc.stderr or ""),
            timed_out=True,
        )
    return RunResult(
        returncode=proc.returncode, stdout=proc.stdout, stderr=proc.stderr, timed_out=False
    )


class Sandbox(Protocol):
    """A backend that runs an argv in isolation and returns its result."""

    name: str

    def run(self, argv: list[str], *, cwd: Path, env: dict[str, str], timeout: int) -> RunResult:
        ...


class RlimitSandbox:
    """Always-available fallback: subprocess + ``resource`` limits + timeout."""

    name = "rlimit"

    def run(self, argv: list[str], *, cwd: Path, env: dict[str, str], timeout: int) -> RunResult:
        return _spawn(argv, cwd=cwd, env=env, timeout=timeout)


class BubblewrapSandbox:
    """Preferred backend: wrap the argv in a ``bwrap`` no-network namespace."""

    name = "bubblewrap"

    # Read-only host paths the sandbox needs to run Python + the tools.
    _RO_PATHS = ("/usr", "/bin", "/lib", "/lib64", "/etc/alternatives", "/etc/ssl")

    def __init__(self, *, bwrap_path: str = "bwrap", backend_root: Path = _BACKEND_ROOT) -> None:
        self.bwrap_path = bwrap_path
        self.backend_root = backend_root

    def wrap(self, argv: list[str], *, cwd: Path, env: dict[str, str]) -> list[str]:
        """Build the ``bwrap`` command line for ``argv`` (pure; unit-tested)."""
        flags: list[str] = [
            self.bwrap_path,
            "--die-with-parent",
            "--new-session",
            "--unshare-all",  # no network, new pid/ipc/uts/user namespaces
            "--clearenv",
        ]
        for key, value in env.items():
            flags += ["--setenv", key, value]
        for ro in (*self._RO_PATHS, sys.prefix, str(self.backend_root)):
            if Path(ro).exists():
                flags += ["--ro-bind", ro, ro]
        flags += [
            "--proc", "/proc",
            "--dev", "/dev",
            "--tmpfs", "/tmp",
            # Bind the (writable) bundle dir AFTER the tmpfs, so a staging dir
            # that lives under /tmp isn't shadowed by the tmpfs mount.
            "--bind", str(cwd), str(cwd),
            "--chdir", str(cwd),
        ]
        return [*flags, "--", *argv]

    def run(self, argv: list[str], *, cwd: Path, env: dict[str, str], timeout: int) -> RunResult:
        wrapped = self.wrap(argv, cwd=cwd, env=env)
        # bwrap applies the namespace isolation; we still pass a minimal outer env
        # and the same wall-clock timeout / rlimits for defense in depth.
        outer_env = {"PATH": env.get("PATH", "/usr/bin:/bin")}
        return _spawn(wrapped, cwd=cwd, env=outer_env, timeout=timeout)


def detect_sandbox(*, backend_root: Path = _BACKEND_ROOT) -> Sandbox:
    """Pick the strongest available sandbox: bubblewrap if present, else rlimit."""
    bwrap = shutil.which("bwrap")
    if bwrap:
        return BubblewrapSandbox(bwrap_path=bwrap, backend_root=backend_root)
    logger.info("bwrap not found; using rlimit sandbox (reduced isolation)")
    return RlimitSandbox()


def _tool(name: str) -> str | None:
    """Resolve a CLI tool, preferring the one beside the running interpreter."""
    candidate = Path(sys.executable).parent / name
    if candidate.exists():
        return str(candidate)
    return shutil.which(name)


def _sandbox_env(backend_root: Path, bundle_dir: Path) -> dict[str, str]:
    """A minimal, secret-free environment for the validation subprocess."""
    return {
        "PATH": os.pathsep.join([str(Path(sys.executable).parent), "/usr/bin", "/bin"]),
        # bundle dir first (so a test can ``import <module>``), then the repo root
        # (so ``app.skills.registry`` / ``pydantic`` resolve).
        "PYTHONPATH": os.pathsep.join([str(bundle_dir), str(backend_root)]),
        "PYTHONDONTWRITEBYTECODE": "1",
        "HOME": "/tmp",
        "LANG": "C.UTF-8",
    }


def _run_check(
    sandbox: Sandbox, name: str, argv: list[str], *, cwd: Path, env: dict[str, str], timeout: int
) -> CheckResult:
    start = time.monotonic()
    res = sandbox.run(argv, cwd=cwd, env=env, timeout=timeout)
    duration_ms = int((time.monotonic() - start) * 1000)
    combined = res.stdout or ""
    if res.stderr:
        combined = f"{combined}\n{res.stderr}" if combined else res.stderr
    return CheckResult(
        name=name,
        ok=(res.returncode == 0 and not res.timed_out),
        skipped=False,
        timed_out=res.timed_out,
        output=_truncate(combined),
        duration_ms=duration_ms,
    )


def _skipped(name: str, why: str) -> CheckResult:
    return CheckResult(name=name, ok=True, skipped=True, timed_out=False, output=why, duration_ms=0)


def run_checks(
    bundle_dir: str | os.PathLike,
    *,
    backend_root: Path = _BACKEND_ROOT,
    sandbox: Sandbox | None = None,
    timeout: int = DEFAULT_TIMEOUT,
    run_tests: bool = True,
) -> ValidationReport:
    """Validate a generated skill bundle in isolation: import → lint → tests.

    Runs each check in ``sandbox`` (auto-detected when not given). ``import``
    loads every non-test module; ``lint`` runs ruff (syntax + pyflakes);
    ``tests`` runs pytest over any ``test_*.py`` in the bundle. A check that
    can't run (e.g. ruff missing, no tests present) is recorded as **skipped**
    (treated as ``ok``). The report is ``ok`` only if no check failed.
    """
    bundle = Path(bundle_dir)
    sandbox = sandbox or detect_sandbox(backend_root=backend_root)
    env = _sandbox_env(backend_root, bundle)
    checks: list[CheckResult] = []

    # 1) import every non-test module (runs @skill decorators).
    checks.append(
        _run_check(
            sandbox,
            "import",
            [sys.executable, "-c", _IMPORT_HARNESS, str(bundle)],
            cwd=bundle,
            env=env,
            timeout=timeout,
        )
    )

    # 2) lint: syntax errors (E9) + pyflakes (F: undefined/unused). Isolated from
    # any repo config so the result is deterministic.
    ruff = _tool("ruff")
    if ruff is None:
        checks.append(_skipped("lint", "ruff not available"))
    else:
        checks.append(
            _run_check(
                sandbox,
                "lint",
                [ruff, "check", "--isolated", "--select", "E9,F", str(bundle)],
                cwd=bundle,
                env=env,
                timeout=timeout,
            )
        )

    # 3) tests: pytest over the bundle's own test files (if any), isolated from
    # the repo's conftest/config so it can't pull in repo fixtures.
    has_tests = any(bundle.glob("test_*.py"))
    if not run_tests:
        checks.append(_skipped("tests", "test run disabled"))
    elif not has_tests:
        checks.append(_skipped("tests", "no test files in bundle"))
    else:
        result = _run_check(
            sandbox,
            "tests",
            [
                sys.executable,
                "-m",
                "pytest",
                "-q",
                "-p",
                "no:cacheprovider",
                "--rootdir",
                str(bundle),
                "--confcutdir",
                str(bundle),
                str(bundle),
            ],
            cwd=bundle,
            env=env,
            timeout=timeout,
        )
        # Tests that are merely *collected and skipped* (e.g. a skill that needs
        # an unavailable third-party package and skips) are NOT a real check.
        # Require at least one test to actually pass.
        if result.ok and "passed" not in result.output:
            note = (
                "\n[validation] tests were collected but none passed (all skipped?). "
                "A generated skill must work with only the Python standard library + "
                "pydantic; do not depend on packages that may be unavailable, and write "
                "tests that actually run and assert behavior rather than skipping."
            )
            result = replace(result, ok=False, output=result.output + note)
        checks.append(result)

    return ValidationReport(ok=all(c.ok for c in checks), checks=tuple(checks))
