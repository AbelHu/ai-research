"""Canonical channel messages + the adapter protocol (design-spec §10).

A `ChannelAdapter` is the only platform-aware code: it parses a raw inbound
payload into an `InboundMessage` and sends an `OutboundMessage`. Everything
downstream (gateway, allowlist, control loop) works on these canonical types, so
the core stays channel-agnostic — Telegram first, Feishu/Teams behind the same
interface.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable


@dataclass(frozen=True)
class InboundMessage:
    """A normalized inbound chat message (design-spec §10).

    ``channel`` + ``channel_user_id`` are the allowlist key (§10.1); ``chat_id``
    is the opaque reply address for that platform (e.g. a Telegram chat id).
    ``raw`` keeps the original payload for audit/debugging.
    """

    channel: str
    channel_user_id: str
    text: str
    chat_id: str
    message_id: str | None = None
    username: str | None = None
    attachments: list[dict] = field(default_factory=list)
    raw: dict = field(default_factory=dict)


@dataclass(frozen=True)
class OutboundMessage:
    """A reply to send back over a channel."""

    channel: str
    chat_id: str
    text: str
    reply_to_message_id: str | None = None


@runtime_checkable
class ChannelAdapter(Protocol):
    """The platform-specific surface the gateway drives (design-spec §10)."""

    name: str

    def parse_inbound(self, raw: dict) -> InboundMessage | None:
        """Normalize a raw platform update into an `InboundMessage`.

        Returns ``None`` for updates that aren't a user text message we handle
        (edits, callbacks, joins, …) — the gateway ignores those.
        """
        ...

    def send(self, msg: OutboundMessage) -> None:
        """Deliver a reply over the platform."""
        ...

    def verify(self, *, secret_token: str | None = None) -> bool:
        """Verify an inbound webhook's authenticity (signature/secret)."""
        ...
