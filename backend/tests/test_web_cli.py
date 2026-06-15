"""Tests for the web service entry point (implementation-plan T10.1).

Offline: the service runs the dashboard **and** a background Telegram gateway in
one process. We cover the gateway-enable decision and the offline bot wiring
(``build_bot``) without binding a socket or touching the network — the autouse
``_no_network`` guard in conftest enforces the latter.
"""

from __future__ import annotations

from types import SimpleNamespace

from app.advisor.wrapper import Advisor
from app.channels.telegram import TelegramAdapter
from app.cli.telegram import build_bot
from app.cli.web import _bot_enabled
from app.config.settings import load_models_config
from app.security import Secret
from app.storage.db import connect
from app.storage.migrations import migrate


def _settings(token: Secret | None, *, webhook: Secret | None = None) -> SimpleNamespace:
    return SimpleNamespace(telegram_bot_token=token, telegram_webhook_secret=webhook)


# --- gateway enable decision ------------------------------------------------


def test_bot_disabled_by_no_bot_flag() -> None:
    # Even with a valid token, --no-bot keeps the gateway off (dashboard only).
    assert _bot_enabled(_settings(Secret("123:abc")), no_bot=True) is False


def test_bot_disabled_without_token() -> None:
    assert _bot_enabled(_settings(None), no_bot=False) is False
    # A present-but-blank token is treated as "not configured".
    assert _bot_enabled(_settings(Secret("   ")), no_bot=False) is False


def test_bot_enabled_with_token() -> None:
    assert _bot_enabled(_settings(Secret("123:abc")), no_bot=False) is True


# --- offline bot wiring -----------------------------------------------------


def test_build_bot_wires_adapter_and_advisor() -> None:
    # build_bot is shared by the standalone runner and the web gateway thread; it
    # must construct the adapter + advisor without any network I/O.
    conn = connect()
    migrate(conn)
    adapter, advisor = build_bot(
        conn, settings=_settings(Secret("123:abc")), models=load_models_config()
    )
    assert isinstance(adapter, TelegramAdapter)
    assert isinstance(advisor, Advisor)
