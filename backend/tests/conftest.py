"""Shared pytest fixtures and offline guards for the backend test suite.

Tests must run fully offline (design-spec O13 / implementation-plan P0): any
attempt to open a real network socket during a test is a bug. We install a
process-wide guard that raises if a test tries to reach the network, so the
suite stays deterministic and CI never depends on GitHub being reachable.

Auditable logs (this module also): every run writes a **detailed, secret-free
log file** under ``backend/logs/`` — one timestamped file per run, labelled
``unit`` or ``integration``. Each test case gets a delimited section with its
start marker, captured application logs (e.g. each advisor model call), the
PASS/FAIL/SKIP result, and the full traceback on failure. A redaction filter
scrubs any secret-looking text from every record, so the files are safe to keep
and share for troubleshooting + validation.
"""

from __future__ import annotations

import datetime
import logging
import socket
from pathlib import Path

import pytest

from app.advisor.redaction import redact_text

# Per-run log files live here (git-ignored). Sibling of the tests/ package.
LOG_DIR = Path(__file__).resolve().parent.parent / "logs"


class NetworkAccessError(RuntimeError):
    """Raised when test code attempts real network access."""


# --- detailed, secret-free per-run logging ---------------------------------


class _SecretRedactingFilter(logging.Filter):
    """Scrub any secret-looking content from a record before it is written.

    Defense-in-depth on top of the `Secret` type + the outbound redaction guard:
    even if some code logs a string that contains a token pattern, the file
    handler rewrites it to ``[REDACTED]`` before writing (§12).
    """

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except Exception:  # noqa: BLE001 - never let logging break a test
            return True
        redacted = redact_text(message)
        if redacted != message:
            record.msg = redacted
            record.args = ()
        return True


def _is_integration_run(config: pytest.Config) -> bool:
    markexpr = config.getoption("markexpr", default="") or ""
    return "integration" in markexpr and "not integration" not in markexpr


def pytest_configure(config: pytest.Config) -> None:
    """Attach a timestamped, secret-redacting log file for this run."""
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    label = "integration" if _is_integration_run(config) else "unit"
    timestamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    log_path = LOG_DIR / f"{label}-{timestamp}.log"

    handler = logging.FileHandler(log_path, encoding="utf-8")
    handler.setLevel(logging.DEBUG)
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s"))
    handler.addFilter(_SecretRedactingFilter())

    root = logging.getLogger()
    root.addHandler(handler)
    if root.level == logging.NOTSET or root.level > logging.DEBUG:
        root.setLevel(logging.DEBUG)
    # Pin the loggers we care about to DEBUG so their records reach the handler
    # regardless of how pytest's own logging plugin sets the root level.
    logging.getLogger("app").setLevel(logging.DEBUG)
    logging.getLogger("pytest").setLevel(logging.DEBUG)

    config._air_log_handler = handler  # type: ignore[attr-defined]
    config._air_log_path = log_path  # type: ignore[attr-defined]
    logging.getLogger("pytest").info("===== run start: %s suite =====", label)


def pytest_runtest_logstart(nodeid: str, location: object) -> None:
    logging.getLogger("pytest").info("----- START %s -----", nodeid)


def pytest_runtest_logreport(report: pytest.TestReport) -> None:
    """Record each test's outcome (+ traceback on failure, reason on skip)."""
    log = logging.getLogger("pytest")
    if report.when == "call" or (report.when == "setup" and report.outcome != "passed"):
        if report.outcome == "skipped":
            reason = ""
            longrepr = report.longrepr
            if isinstance(longrepr, tuple) and len(longrepr) == 3:
                reason = longrepr[2]
            log.info("SKIPPED %s (%s)", report.nodeid, reason)
            return
        level = logging.ERROR if report.failed else logging.INFO
        log.log(level, "%s %s (%.3fs)", report.outcome.upper(), report.nodeid, report.duration)
        if report.failed and getattr(report, "longreprtext", ""):
            log.error("TRACEBACK %s:\n%s", report.nodeid, report.longreprtext)


def pytest_unconfigure(config: pytest.Config) -> None:
    handler = getattr(config, "_air_log_handler", None)
    if handler is not None:
        logging.getLogger("pytest").info("===== run end =====")
        logging.getLogger().removeHandler(handler)
        handler.close()


def pytest_terminal_summary(terminalreporter, exitstatus, config: pytest.Config) -> None:  # noqa: ANN001
    path = getattr(config, "_air_log_path", None)
    if path is not None:
        terminalreporter.write_line(f"Detailed log written to: {path}")


@pytest.fixture(autouse=True)
def _no_network(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> None:
    """Block real socket connections for every test.

    Tests that need a provider must use a fake transport / fake provider
    instead of touching the network. The **only** exception is the opt-in
    ``integration`` suite (marked ``@pytest.mark.integration``), which calls a
    real model and is excluded from the default run — it is allowed past this
    guard so live tests can actually reach the network.
    """
    if request.node.get_closest_marker("integration") is not None:
        return  # live integration test: real network is permitted

    def _blocked(*args: object, **kwargs: object) -> None:
        raise NetworkAccessError(
            "Network access is disabled in tests. Use a fake provider or "
            "mock the httpx transport instead."
        )

    monkeypatch.setattr(socket.socket, "connect", _blocked)


@pytest.fixture
def skill_registry():
    """Snapshot/restore the process-wide skill REGISTRY.

    Tests that register throwaway skills request this so their dummies don't
    leak into (or collide within) the real catalog. The real skills registered
    at import are preserved across the snapshot/restore.
    """
    from app.skills.registry import REGISTRY

    saved = dict(REGISTRY)
    try:
        yield REGISTRY
    finally:
        REGISTRY.clear()
        REGISTRY.update(saved)
