"""Tests for the policy-knobs loader (implementation-plan T0.5).

Verifies the documented defaults load, the shipped `policies.yaml` parses and
matches those defaults, partial overrides win over defaults, and invalid
values / unknown keys are rejected.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from app.config.policies import (
    DEFAULT_POLICIES_CONFIG,
    Policies,
    load_policies,
)


def _write(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "policies.yaml"
    path.write_text(text, encoding="utf-8")
    return path


def test_defaults_match_spec() -> None:
    p = Policies()
    assert p.max_phase_declines == 3
    assert p.max_improvement_iterations == 2
    assert p.max_append_reroutes == 1
    assert p.max_concurrent_jobs == 3
    assert p.max_job_retries == 1
    assert p.max_task_steps == 3
    assert p.verify_success_criteria is True
    assert p.max_replan_attempts == 1
    assert p.coder_validate is True
    assert p.max_coder_iterations == 2
    assert p.coder_sandbox_timeout_seconds == 30
    assert p.junior_session_idle_minutes == 15
    assert p.progress_updates == "phase"
    assert p.verify_citation_urls is True  # cited-URL check ships ON (§7.1)


def test_shipped_policies_load_to_defaults() -> None:
    # The shipped file documents the defaults; loading it must equal them.
    assert load_policies(DEFAULT_POLICIES_CONFIG) == Policies()


def test_missing_file_yields_defaults(tmp_path: Path) -> None:
    missing = tmp_path / "nope.yaml"
    assert load_policies(missing) == Policies()


def test_empty_file_yields_defaults(tmp_path: Path) -> None:
    assert load_policies(_write(tmp_path, "")) == Policies()


def test_partial_override_wins(tmp_path: Path) -> None:
    cfg = load_policies(_write(tmp_path, "max_concurrent_jobs: 5\nprogress_updates: task\n"))
    assert cfg.max_concurrent_jobs == 5
    assert cfg.progress_updates == "task"
    # Unspecified keys keep their defaults.
    assert cfg.max_phase_declines == 3


def test_verify_citation_urls_can_be_disabled(tmp_path: Path) -> None:
    cfg = load_policies(_write(tmp_path, "verify_citation_urls: false\n"))
    assert cfg.verify_citation_urls is False
    # Other knobs keep their defaults.
    assert cfg.max_concurrent_jobs == 3


def test_rejects_unknown_key(tmp_path: Path) -> None:
    with pytest.raises(ValidationError):
        load_policies(_write(tmp_path, "max_concurency_jobs: 4\n"))


def test_rejects_out_of_range(tmp_path: Path) -> None:
    with pytest.raises(ValidationError):
        load_policies(_write(tmp_path, "max_concurrent_jobs: 0\n"))


def test_rejects_bad_progress_value(tmp_path: Path) -> None:
    with pytest.raises(ValidationError):
        load_policies(_write(tmp_path, "progress_updates: hourly\n"))


def test_policies_are_frozen() -> None:
    p = Policies()
    with pytest.raises(ValidationError):
        p.max_concurrent_jobs = 9
