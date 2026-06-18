"""The agentic Coder loop — generate → validate → repair, bounded (P3).

Turns a feature goal into a **validated, inert** skill bundle without a human in
the loop, while keeping the safety model:

1. ask the advisor for a :class:`GeneratedSkillBundle` (model proposes code only);
2. write it to a throwaway **staging** dir and run the sandbox checks
   (:func:`app.coder.sandbox.run_checks` — import + lint + tests);
3. on failure, feed the captured sandbox output back to the advisor for a
   bounded **repair** and retry (up to ``max_coder_iterations``);
4. on pass, **promote** the bundle to the real generated root as ``inert`` —
   activation still requires explicit user confirmation (``confirm_generated_code``).

Deterministic code owns the loop, the file writes, and the sandbox; the model
only writes code and reads failures. The validation step and the advisor are
injectable so the loop is unit-testable without spawning real subprocesses.
"""

from __future__ import annotations

import logging
import tempfile
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from app.advisor.schemas import GeneratedSkillBundle
from app.advisor.wrapper import Advisor, AdvisorValidationError
from app.coder.sandbox import Sandbox, ValidationReport, detect_sandbox, run_checks
from app.config.policies import get_policies
from app.skills import codegen

logger = logging.getLogger("app.coder.agent")

# A validation seam: given a bundle directory, return its report. Injected in
# tests; defaults to the real sandboxed `run_checks`.
BundleChecker = Callable[[Path], ValidationReport]


@dataclass(frozen=True)
class CoderOutcome:
    """The result of one agentic Coder run for a feature job."""

    ok: bool
    job_code: str
    iterations: int
    skill_modules: list[str] = field(default_factory=list)
    rationale: str = ""
    report: ValidationReport | None = None
    error: str | None = None


def _files(bundle: GeneratedSkillBundle) -> list[tuple[str, str]]:
    return [(f.filename, f.code) for f in bundle.files]


def _tests(bundle: GeneratedSkillBundle) -> list[tuple[str, str]]:
    return [(f.filename, f.code) for f in bundle.test_files]


def _render_previous(bundle: GeneratedSkillBundle) -> str:
    """Render the prior bundle's files for the repair prompt."""
    return "\n\n".join(
        f"# {f.filename}\n{f.code}" for f in [*bundle.files, *bundle.test_files]
    )


def _format_failures(report: ValidationReport) -> str:
    """Render the failing checks' captured output for the repair prompt."""
    parts: list[str] = []
    for check in report.checks:
        if check.ok or check.skipped:
            continue
        tag = f"[{check.name}]" + (" (timed out)" if check.timed_out else "")
        parts.append(f"{tag}\n{check.output}")
    return "\n\n".join(parts) or "validation failed"


def run_coder(
    advisor: Advisor,
    *,
    job_code: str,
    goal: str,
    request_id: int,
    job_id: int | None = None,
    generated_root: Path | None = None,
    sandbox: Sandbox | None = None,
    max_iterations: int | None = None,
    timeout: int | None = None,
    validate: bool | None = None,
    check_bundle: BundleChecker | None = None,
) -> CoderOutcome:
    """Generate + validate + (bounded) repair a feature job's skill bundle.

    On success the validated bundle is written **inert** to ``generated_root``
    (default: the real generated package) and ``ok=True``; activation remains a
    separate, user-confirmed step. ``check_bundle`` overrides the validation seam
    (tests); ``validate=False`` writes the first bundle inert without checks.
    """
    policies = get_policies()
    max_iters = max_iterations if max_iterations is not None else policies.max_coder_iterations
    do_validate = validate if validate is not None else policies.coder_validate
    timeout = timeout if timeout is not None else policies.coder_sandbox_timeout_seconds
    gen_root = generated_root if generated_root is not None else codegen.GENERATED_ROOT
    sandbox = sandbox or detect_sandbox()
    checker = check_bundle or (lambda d: run_checks(d, sandbox=sandbox, timeout=timeout))

    try:
        bundle = advisor.generate_bundle(goal=goal, request_id=request_id, job_id=job_id)
    except AdvisorValidationError as exc:
        return CoderOutcome(ok=False, job_code=job_code, iterations=0, error=str(exc))

    if not do_validate:
        written = codegen.write_generated_bundle(
            gen_root, job_code, files=_files(bundle), test_files=_tests(bundle)
        )
        return CoderOutcome(
            ok=True,
            job_code=job_code,
            iterations=1,
            skill_modules=[p.name for p in written],
            rationale=bundle.rationale,
        )

    last_report: ValidationReport | None = None
    for attempt in range(1, max_iters + 1):
        with tempfile.TemporaryDirectory(prefix="coder-stage-") as staging:
            codegen.write_generated_bundle(
                Path(staging), job_code, files=_files(bundle), test_files=_tests(bundle)
            )
            report = checker(Path(staging) / job_code)
        last_report = report

        if report.ok:
            written = codegen.write_generated_bundle(
                gen_root, job_code, files=_files(bundle), test_files=_tests(bundle)
            )
            logger.info("coder job %s validated on attempt %s/%s", job_code, attempt, max_iters)
            return CoderOutcome(
                ok=True,
                job_code=job_code,
                iterations=attempt,
                skill_modules=[p.name for p in written],
                rationale=bundle.rationale,
                report=report,
            )

        if attempt >= max_iters:
            break

        logger.info(
            "coder job %s failed validation (attempt %s): %s — repairing",
            job_code,
            attempt,
            report.summary,
        )
        try:
            bundle = advisor.repair_bundle(
                goal=goal,
                previous_code=_render_previous(bundle),
                failures=_format_failures(report),
                request_id=request_id,
                job_id=job_id,
            )
        except AdvisorValidationError as exc:
            return CoderOutcome(
                ok=False, job_code=job_code, iterations=attempt, report=last_report, error=str(exc)
            )

    return CoderOutcome(
        ok=False,
        job_code=job_code,
        iterations=max_iters,
        report=last_report,
        error="validation failed after retries",
    )
