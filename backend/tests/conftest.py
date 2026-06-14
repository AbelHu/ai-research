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
def _no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """Block real socket connections for every test.

    Tests that need a provider must use a fake transport / fake provider
    instead of touching the network.
    """

    def _blocked(*args: object, **kwargs: object) -> None:
        raise NetworkAccessError(
            "Network access is disabled in tests. Use a fake provider or "
            "mock the httpx transport instead."
        )

    monkeypatch.setattr(socket.socket, "connect", _blocked)
