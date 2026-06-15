"""Telegram channel adapter (design-spec §10; implementation-plan T8.2).

Wraps the Telegram **Bot API** behind the canonical `ChannelAdapter` surface:

* ``parse_inbound`` — a raw ``getUpdates``/webhook update → `InboundMessage`
  (only plain user text messages; everything else returns ``None``).
* ``send`` — POST ``sendMessage`` with the reply text.
* ``get_updates`` — long-poll ``getUpdates`` (offset-acknowledged) for the
  runner in `app/cli/telegram.py`.
* ``verify`` — constant-time check of the webhook secret header (webhook mode).

The HTTP client is injectable so the whole adapter is unit-tested offline
against a mocked transport (no network). The bot token is wrapped in `Secret`
and only revealed at the HTTP boundary (§12) — it never lands in logs/URLs we
emit for auditing.
"""

from __future__ import annotations

import hmac
from collections.abc import Callable
from functools import partial

import httpx

from app.channels.adapter import InboundMessage, OutboundMessage
from app.security import Secret

CHANNEL_NAME = "telegram"
API_BASE = "https://api.telegram.org"
# Long-poll seconds passed to getUpdates; the HTTP read timeout adds headroom.
DEFAULT_POLL_TIMEOUT = 30
# Seconds added on top of the long-poll window for the HTTP client timeout, so an
# idle getUpdates (Telegram holds the connection up to ``poll_timeout`` seconds
# when nothing is pending) isn't aborted by the client before it returns.
POLL_TIMEOUT_HEADROOM = 15

ClientFactory = Callable[[], httpx.Client]


class TelegramError(RuntimeError):
    """A Telegram Bot API call failed (non-``ok`` response or HTTP error)."""


class TelegramAdapter:
    """Telegram Bot API adapter (canonical `ChannelAdapter`)."""

    name = CHANNEL_NAME

    def __init__(
        self,
        token: str | Secret,
        *,
        client_factory: ClientFactory | None = None,
        api_base: str = API_BASE,
        webhook_secret: str | Secret | None = None,
        poll_timeout: int = DEFAULT_POLL_TIMEOUT,
    ) -> None:
        self._token = token if isinstance(token, Secret) else Secret(token)
        self._poll_timeout = poll_timeout
        # The long-poll holds the HTTP connection open up to ``poll_timeout``
        # seconds when no updates are pending; the client read timeout must
        # exceed that or every idle getUpdates aborts (the bot then exits ~6s in,
        # at httpx's 5s default). Default to poll_timeout + headroom; an injected
        # factory (tests) is used as-is.
        if client_factory is None:
            client_factory = partial(
                httpx.Client, timeout=float(poll_timeout + POLL_TIMEOUT_HEADROOM)
            )
        self._client_factory = client_factory
        self._api_base = api_base.rstrip("/")
        self._webhook_secret = (
            webhook_secret
            if webhook_secret is None or isinstance(webhook_secret, Secret)
            else Secret(webhook_secret)
        )

    # --- inbound ------------------------------------------------------------

    def parse_inbound(self, raw: dict) -> InboundMessage | None:
        """Normalize a Telegram update; return ``None`` for non-text/non-message updates."""
        message = raw.get("message") or raw.get("edited_message")
        if not isinstance(message, dict):
            return None
        text = message.get("text")
        sender = message.get("from")
        chat = message.get("chat")
        if not text or not isinstance(sender, dict) or not isinstance(chat, dict):
            return None
        sender_id = sender.get("id")
        chat_id = chat.get("id")
        if sender_id is None or chat_id is None:
            return None
        return InboundMessage(
            channel=self.name,
            channel_user_id=str(sender_id),
            text=text,
            chat_id=str(chat_id),
            message_id=str(message["message_id"])
            if message.get("message_id") is not None
            else None,
            username=sender.get("username"),
            raw=raw,
        )

    # --- outbound -----------------------------------------------------------

    def send(self, msg: OutboundMessage) -> None:
        """Send a reply via ``sendMessage``."""
        payload: dict = {"chat_id": msg.chat_id, "text": msg.text}
        if msg.reply_to_message_id is not None:
            payload["reply_to_message_id"] = msg.reply_to_message_id
        self._call("sendMessage", payload)

    def get_me(self) -> dict:
        """Return the bot's own account via ``getMe`` (token check during setup).

        Raises `TelegramError` if the token is rejected, so the setup wizard can
        confirm a freshly-entered ``TELEGRAM_BOT_TOKEN`` actually works.
        """
        result = self._call("getMe", {})
        return result if isinstance(result, dict) else {}

    # --- long-poll ----------------------------------------------------------

    def get_updates(self, *, offset: int | None = None) -> list[dict]:
        """Fetch pending updates (long-poll). ``offset`` acknowledges prior updates."""
        payload: dict = {"timeout": self._poll_timeout}
        if offset is not None:
            payload["offset"] = offset
        result = self._call("getUpdates", payload)
        return result if isinstance(result, list) else []

    # --- webhook verification ----------------------------------------------

    def verify(self, *, secret_token: str | None = None) -> bool:
        """Constant-time check of the webhook secret header (webhook mode).

        With no configured secret, verification is a no-op pass (long-poll mode,
        where updates are pulled by us and need no inbound authentication).
        """
        if self._webhook_secret is None:
            return True
        if secret_token is None:
            return False
        return hmac.compare_digest(secret_token, self._webhook_secret.reveal())

    # --- internals ----------------------------------------------------------

    def _url(self, method: str) -> str:
        return f"{self._api_base}/bot{self._token.reveal()}/{method}"

    def _call(self, method: str, payload: dict) -> object:
        try:
            with self._client_factory() as client:
                resp = client.post(self._url(method), json=payload)
                resp.raise_for_status()
                data = resp.json()
        except httpx.HTTPError as exc:
            raise TelegramError(f"Telegram {method} failed: {exc}") from exc
        if not data.get("ok"):
            raise TelegramError(f"Telegram {method} returned not-ok: {data.get('description')}")
        return data.get("result")
