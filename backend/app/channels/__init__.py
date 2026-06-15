"""Channel adapters — normalize chat platforms to a canonical message (design-spec §10).

The core never knows platform specifics: each adapter turns a raw inbound
platform payload into an `InboundMessage` and sends an `OutboundMessage` back.
Identity resolution + the paired-owner allowlist (§10.1) happen in the gateway
on the canonical `(channel, channel_user_id)` — not here.
"""

from app.channels.adapter import ChannelAdapter, InboundMessage, OutboundMessage

__all__ = ["ChannelAdapter", "InboundMessage", "OutboundMessage"]
