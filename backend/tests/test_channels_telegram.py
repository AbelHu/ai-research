"""Tests for the Telegram channel adapter (implementation-plan T8.1/T8.2).

Offline: a recorded update fixture + an ``httpx.MockTransport`` for send/getUpdates.
No network (the conftest guard still applies).
"""

from __future__ import annotations

import httpx
import pytest

from app.channels.adapter import InboundMessage, OutboundMessage
from app.channels.telegram import API_BASE, TelegramAdapter, TelegramError

# A recorded Telegram `getUpdates` message update.
UPDATE_MESSAGE = {
    "update_id": 100,
    "message": {
        "message_id": 7,
        "from": {"id": 42, "username": "owner", "first_name": "O"},
        "chat": {"id": 4242, "type": "private"},
        "text": "what is 2+2?",
    },
}


def _adapter(handler, *, token="bot-token", **kwargs) -> TelegramAdapter:
    return TelegramAdapter(
        token,
        client_factory=lambda: httpx.Client(transport=httpx.MockTransport(handler)),
        **kwargs,
    )


# --- parse_inbound ----------------------------------------------------------


def test_parse_inbound_normalizes_a_message() -> None:
    adapter = _adapter(lambda r: httpx.Response(200, json={"ok": True}))
    inbound = adapter.parse_inbound(UPDATE_MESSAGE)
    assert isinstance(inbound, InboundMessage)
    assert inbound.channel == "telegram"
    assert inbound.channel_user_id == "42"  # from.id, stringified (allowlist key)
    assert inbound.chat_id == "4242"  # reply address
    assert inbound.text == "what is 2+2?"
    assert inbound.message_id == "7"
    assert inbound.username == "owner"
    assert inbound.raw is UPDATE_MESSAGE


def test_parse_inbound_ignores_non_message_updates() -> None:
    adapter = _adapter(lambda r: httpx.Response(200, json={"ok": True}))
    assert adapter.parse_inbound({"update_id": 1, "callback_query": {"id": "x"}}) is None
    # A message with no text (e.g. a photo) is ignored.
    no_text = {"update_id": 2, "message": {"message_id": 1, "from": {"id": 1}, "chat": {"id": 1}}}
    assert adapter.parse_inbound(no_text) is None


def test_parse_inbound_handles_edited_message() -> None:
    adapter = _adapter(lambda r: httpx.Response(200, json={"ok": True}))
    edited = {"update_id": 3, "edited_message": UPDATE_MESSAGE["message"]}
    inbound = adapter.parse_inbound(edited)
    assert inbound is not None and inbound.text == "what is 2+2?"


# --- send -------------------------------------------------------------------


def test_send_posts_sendmessage_with_text() -> None:
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        import json as _json

        seen["url"] = str(request.url)
        seen["payload"] = _json.loads(request.content)
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 99}})

    adapter = _adapter(handler, token="SEKRET")
    adapter.send(
        OutboundMessage(channel="telegram", chat_id="4242", text="hi", reply_to_message_id="7")
    )

    assert seen["url"] == f"{API_BASE}/botSEKRET/sendMessage"
    assert seen["payload"] == {"chat_id": "4242", "text": "hi", "reply_to_message_id": "7"}


def test_send_raises_on_not_ok() -> None:
    adapter = _adapter(lambda r: httpx.Response(200, json={"ok": False, "description": "blocked"}))
    with pytest.raises(TelegramError):
        adapter.send(OutboundMessage(channel="telegram", chat_id="1", text="x"))


# --- get_updates ------------------------------------------------------------


def test_get_updates_passes_offset_and_returns_list() -> None:
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        import json as _json

        seen["payload"] = _json.loads(request.content)
        return httpx.Response(200, json={"ok": True, "result": [UPDATE_MESSAGE]})

    adapter = _adapter(handler)
    updates = adapter.get_updates(offset=101)
    assert updates == [UPDATE_MESSAGE]
    assert seen["payload"]["offset"] == 101
    assert "timeout" in seen["payload"]


def test_default_client_timeout_exceeds_poll_timeout() -> None:
    # The default HTTP client must outlast the long-poll window, or an idle
    # getUpdates aborts ~6s in (httpx's 5s default) and the bot exits — the bug
    # that killed the service shortly after start.
    adapter = TelegramAdapter("bot-token", poll_timeout=30)
    client = adapter._client_factory()
    try:
        assert client.timeout.read is not None
        assert client.timeout.read > 30
    finally:
        client.close()


# --- verify (webhook secret) ------------------------------------------------


def test_verify_noop_without_secret() -> None:
    adapter = _adapter(lambda r: httpx.Response(200, json={"ok": True}))
    assert adapter.verify(secret_token=None) is True  # long-poll mode: no secret


def test_verify_checks_secret_constant_time() -> None:
    adapter = _adapter(lambda r: httpx.Response(200, json={"ok": True}), webhook_secret="s3cret")
    assert adapter.verify(secret_token="s3cret") is True
    assert adapter.verify(secret_token="wrong") is False
    assert adapter.verify(secret_token=None) is False


def test_token_not_in_repr() -> None:
    adapter = _adapter(lambda r: httpx.Response(200, json={"ok": True}), token="super-secret-token")
    assert "super-secret-token" not in repr(adapter._token)
