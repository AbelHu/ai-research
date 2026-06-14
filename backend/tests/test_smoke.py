"""Trivial smoke test: the package imports and the harness runs."""

from __future__ import annotations


def test_app_package_imports() -> None:
    import app  # noqa: F401


def test_truth() -> None:
    assert True
