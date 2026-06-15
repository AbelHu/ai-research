"""Tests for the CLI/runtime run-logging helper (`app.runlog`).

Offline: exercises file-handler creation, secret redaction, and logger setup
without touching the network.
"""

from __future__ import annotations

import logging

import pytest

from app import runlog


@pytest.fixture
def app_logger_isolation():
    """Snapshot the ``app`` logger so handlers added by a test don't leak."""
    logger = logging.getLogger("app")
    saved_handlers = list(logger.handlers)
    saved_level = logger.level
    try:
        yield logger
    finally:
        for handler in list(logger.handlers):
            if handler not in saved_handlers:
                handler.close()
                logger.removeHandler(handler)
        logger.setLevel(saved_level)


def test_setup_run_logging_creates_redacted_log_file(
    tmp_path, monkeypatch, app_logger_isolation
) -> None:
    monkeypatch.setattr(runlog, "LOG_DIR", tmp_path)

    log_path = runlog.setup_run_logging("ask")

    # File created under the (patched) log dir, app logger raised to DEBUG.
    assert log_path.parent == tmp_path
    assert log_path.name.startswith("ask-")
    assert app_logger_isolation.level == logging.DEBUG

    # A secret-looking token logged through the app logger is scrubbed on disk.
    fake_secret = "ghp_" + "a" * 36
    logging.getLogger("app.advisor").info("advisor response: token=%s end", fake_secret)
    for handler in app_logger_isolation.handlers:
        handler.flush()

    contents = log_path.read_text(encoding="utf-8")
    assert fake_secret not in contents
    assert "[REDACTED]" in contents
    assert "advisor response:" in contents


def test_setup_run_logging_console_echo_optional(
    tmp_path, monkeypatch, app_logger_isolation
) -> None:
    monkeypatch.setattr(runlog, "LOG_DIR", tmp_path)

    # Default: file handler only.
    runlog.setup_run_logging("ask")
    stream_handlers = [
        h
        for h in app_logger_isolation.handlers
        if isinstance(h, logging.StreamHandler) and not isinstance(h, logging.FileHandler)
    ]
    assert stream_handlers == []

    # With console_level: a console stream handler is added at that level.
    runlog.setup_run_logging("ask", console_level=logging.INFO)
    stream_handlers = [
        h
        for h in app_logger_isolation.handlers
        if isinstance(h, logging.StreamHandler) and not isinstance(h, logging.FileHandler)
    ]
    assert len(stream_handlers) == 1
    assert stream_handlers[0].level == logging.INFO
