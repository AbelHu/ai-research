"""Tests for the Telegram long-poll runner loop (``app.cli.telegram.serve``).

Offline: a fake adapter drives ``serve`` without any network. These pin the
**service resilience** contract — a transient long-poll error (timeout, network
blip, Telegram 5xx) must NOT exit the loop; the gateway keeps listening.
"""

from __future__ import annotations

import pytest

from app.advisor.wrapper import Advisor
from app.channels.telegram import TelegramError
from app.cli.telegram import ERROR_BACKOFF_SECONDS, serve
from app.storage.db import connect
from app.storage.migrations import migrate
from tests.fakes import FakeProvider


class _StopLoop(Exception):
    """Sentinel (not a TelegramError) used to break serve's infinite loop."""


def _advisor(conn) -> Advisor:
    return Advisor(resolve_provider=lambda _role: FakeProvider("{}"), conn=conn)


@pytest.fixture
def conn():
    c = connect()
    migrate(c)
    try:
        yield c
    finally:
        c.close()


class _FlakyAdapter:
    """get_updates: transient error → empty poll → stop. Tracks call count."""

    def __init__(self) -> None:
        self.calls = 0

    def get_updates(self, *, offset=None):
        self.calls += 1
        if self.calls == 1:
            raise TelegramError("simulated read timeout (~6s)")
        if self.calls == 2:
            return []  # nothing pending — serve must keep going, not exit
        raise _StopLoop()  # break out of the otherwise-infinite service loop

    def parse_inbound(self, update):  # pragma: no cover - no updates delivered
        return None

    def send(self, msg):  # pragma: no cover - never reached
        raise AssertionError("send should not be called")


def test_serve_survives_transient_error_and_keeps_listening(conn) -> None:
    adapter = _FlakyAdapter()
    sleeps: list[float] = []

    with pytest.raises(_StopLoop):
        serve(conn, adapter, _advisor(conn), on_error_sleep=sleeps.append)

    # The error did NOT exit the loop: it backed off once, then polled again.
    assert adapter.calls == 3
    assert sleeps == [ERROR_BACKOFF_SECONDS]


def test_serve_once_returns_1_on_error(conn) -> None:
    class _Err:
        def get_updates(self, *, offset=None):
            raise TelegramError("boom")

    # In --once (smoke) mode the failure is surfaced as a non-zero exit instead.
    rc = serve(conn, _Err(), _advisor(conn), once=True, on_error_sleep=lambda _s: None)
    assert rc == 1
