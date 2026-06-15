"""Gateway / Ingress (design-spec §6A, §10.1; implementation-plan T7.4).

The Gateway is the deterministic front door for every inbound chat message. Its
first job is **access control**: a chat bot is publicly reachable, so only the
**paired owner** may drive the system — every unpaired sender is refused, the
refusal is **audited**, and refusals are **rate-limited** to resist probing
(§10.1). The model is never consulted on who may chat.
"""

from app.gateway.allowlist import (
    AllowDecision,
    RefusalRateLimiter,
    check_inbound,
)
from app.gateway.ingress import IngressResult, handle_inbound, parse_pair_command
from app.gateway.pairing import (
    PairResult,
    bind_verified_owner,
    pair_with_host_code,
    run_device_flow_challenge,
)

__all__ = [
    "AllowDecision",
    "IngressResult",
    "PairResult",
    "RefusalRateLimiter",
    "bind_verified_owner",
    "check_inbound",
    "handle_inbound",
    "pair_with_host_code",
    "parse_pair_command",
    "run_device_flow_challenge",
]
