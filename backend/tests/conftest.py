"""Shared pytest fixtures and offline guards for the backend test suite.

Tests must run fully offline (design-spec O13 / implementation-plan P0): any
attempt to open a real network socket during a test is a bug. We install a
process-wide guard that raises if a test tries to reach the network, so the
suite stays deterministic and CI never depends on GitHub being reachable.
"""

from __future__ import annotations

import socket

import pytest


class NetworkAccessError(RuntimeError):
    """Raised when test code attempts real network access."""


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
