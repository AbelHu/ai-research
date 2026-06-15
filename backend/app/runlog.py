"""Run logging for CLI/runtime entry points (design-spec §12 — auditable + redacted).

Writes a timestamped DEBUG log under ``backend/logs/`` with a secret-redacting
filter so every advisor call — including the **full model response** — is
captured for later inspection without leaking credentials. Optionally mirrors a
chosen level to the console for live viewing.

The test harness has its own (richer) logging setup in ``tests/conftest.py``;
this module is the equivalent for live entry points like ``app.cli.ask``.
"""

from __future__ import annotations

import datetime
import logging
from pathlib import Path

from app.advisor.redaction import redact_text

LOG_DIR = Path(__file__).resolve().parent.parent / "logs"

_LOG_FORMAT = "%(asctime)s %(levelname)-7s %(name)s: %(message)s"


class SecretRedactingFilter(logging.Filter):
    """Scrub any secret-looking content from a record before it is written (§12).

    Defense-in-depth on top of the ``Secret`` type and the outbound redaction
    guard: even if some code logs a string holding a token pattern, the handler
    rewrites it to ``[REDACTED]`` before it reaches disk or the console.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except Exception:  # noqa: BLE001 - never let logging break the program
            return True
        redacted = redact_text(message)
        if redacted != message:
            record.msg = redacted
            record.args = ()
        return True


def setup_run_logging(name: str, *, console_level: int | None = None) -> Path:
    """Attach a redacted DEBUG file log (plus optional console echo); return its path.

    Raises the ``app`` logger to DEBUG so the advisor's INFO lines — which now
    include the model's full (redacted) response — land in the file. Returns the
    log path so the caller can tell the user exactly where to look.
    """
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    log_path = LOG_DIR / f"{name}-{timestamp}.log"

    formatter = logging.Formatter(_LOG_FORMAT)
    redactor = SecretRedactingFilter()
    app_logger = logging.getLogger("app")
    app_logger.setLevel(logging.DEBUG)

    file_handler = logging.FileHandler(log_path, encoding="utf-8")
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(formatter)
    file_handler.addFilter(redactor)
    app_logger.addHandler(file_handler)

    if console_level is not None:
        console = logging.StreamHandler()
        console.setLevel(console_level)
        console.setFormatter(formatter)
        console.addFilter(redactor)
        app_logger.addHandler(console)

    return log_path
