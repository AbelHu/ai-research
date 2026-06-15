"""Gateway ingress — inbound chat message → allowlist → control loop (T8.3/T8.4).

The deterministic front door that ties P7 (pairing/allowlist) to P4 (the ask
control loop) for any channel:

  1. **`/pair <code>`** (from anyone) → spend a host one-time code → bind the
     sender to the owner (§10.1). This is the chat side of `app/cli/pair.py`.
  2. **Allowlist check** — a paired sender proceeds; an unpaired/revoked sender
     is **refused** (no request/job created), audited, rate-limited, and — per
     policy — gets a single "pair first" hint (§10.1).
  3. **Admitted →** drive the message through `run_ask` and format the outcome
     into a reply.

Pure-ish: `handle_inbound` returns the reply (an `OutboundMessage` or ``None``)
rather than sending it, so it's fully testable; the runner (`app/cli/telegram.py`)
does the actual `adapter.send`.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from app.advisor.wrapper import Advisor
from app.channels.adapter import InboundMessage, OutboundMessage
from app.config.policies import Policies, get_policies
from app.gateway.allowlist import RefusalRateLimiter, check_inbound
from app.gateway.pairing import pair_with_host_code, request_pairing
from app.roles.control import AskOutcome, run_ask
from app.storage.repos import job_queue as job_queue_repo

# Reply copy (kept deterministic + free of internal ids/secrets).
PAIR_HINT = (
    "You're not paired with this assistant yet. To pair, ask the operator for a "
    "one-time code and send:  /pair <code>"
)
PAIR_OK = "Paired — you can chat now."
PAIR_BAD_CODE = "That pairing code is invalid or expired. Ask the operator for a fresh one."
PAIR_USAGE = "Usage: /pair <code>"


def _pair_request_message(code: str) -> str:
    """The reply an unpaired sender gets: their claim code + how to get approved."""
    return (
        "You're not paired with this assistant yet.\n"
        f"Your pairing code is: {code}\n"
        f"Ask the operator to approve it by running:  pair --approve {code}"
    )


@dataclass(frozen=True)
class IngressResult:
    """What the gateway decided for one inbound message (for the runner + tests)."""

    action: str  # "answered" | "paired" | "bad_code" | "refused" | "ignored"
    reply: OutboundMessage | None
    user_id: int | None = None


def _reply(inbound: InboundMessage, text: str) -> OutboundMessage:
    return OutboundMessage(
        channel=inbound.channel,
        chat_id=inbound.chat_id,
        text=text,
        reply_to_message_id=inbound.message_id,
    )


def parse_pair_command(text: str) -> str | None:
    """Return the code from a ``/pair <code>`` message, or ``None`` if not one.

    Tolerant of a trailing ``@botname`` on the command (Telegram group style)
    and surrounding whitespace. ``/pair`` with no code returns the empty string
    (so the handler can show usage).
    """
    stripped = text.strip()
    head, _, rest = stripped.partition(" ")
    command = head.split("@", 1)[0].lower()
    if command != "/pair":
        return None
    return rest.strip()


def _format_outcome(outcome: AskOutcome) -> str:
    """Render an `AskOutcome` as a single chat reply (mirrors the `ask` CLI)."""
    if outcome.status in ("answered", "unanswered"):
        return outcome.delivery or ""
    if outcome.status == "needs_clarification":
        lines = [f"/req {outcome.request.code} needs clarification:"]
        lines += [f"  - {q}" for q in (outcome.clarify or [])]
        return "\n".join(lines)
    if outcome.status == "planned":
        return (
            f"/req {outcome.request.code} is a complex job (job #{outcome.job_id}); "
            "I'll work through it and report back."
        )
    return f"/req {outcome.request.code} could not be routed; please rephrase."


def handle_inbound(
    conn: sqlite3.Connection,
    inbound: InboundMessage,
    *,
    advisor: Advisor,
    policy: Policies | None = None,
    rate_limiter: RefusalRateLimiter | None = None,
) -> IngressResult:
    """Process one inbound message end-to-end; return the reply to send (T8.3/T8.4).

    Order matters: ``/pair`` is handled **before** the allowlist so an unpaired
    sender can pair themselves; everything else requires a paired identity.
    """
    policy = policy or get_policies()

    # 1) `/pair <code>` — the chat side of host-code pairing (§10.1).
    pair_code = parse_pair_command(inbound.text)
    if pair_code is not None:
        if not pair_code:
            return IngressResult("bad_code", _reply(inbound, PAIR_USAGE))
        result = pair_with_host_code(
            conn,
            code=pair_code,
            channel=inbound.channel,
            channel_user_id=inbound.channel_user_id,
        )
        if result.paired:
            return IngressResult(
                "paired", _reply(inbound, PAIR_OK), user_id=result.identity.user_id
            )
        return IngressResult("bad_code", _reply(inbound, PAIR_BAD_CODE))

    # 2) Allowlist: only a paired sender may drive the system (§10.1).
    decision = check_inbound(
        conn,
        inbound.channel,
        inbound.channel_user_id,
        policy=policy,
        rate_limiter=rate_limiter,
    )
    if not decision.admitted:
        # An unpaired sender gets a request-and-approve **pairing code** to hand
        # to the operator (still no request/job; rate-limited + audited upstream).
        # A revoked sender is just refused (no fresh code). Once the limiter trips,
        # `should_reply` is false → we stay silent (no code spam).
        if decision.should_reply and decision.reason == "unpaired":
            code = request_pairing(
                conn, channel=inbound.channel, channel_user_id=inbound.channel_user_id
            )
            return IngressResult("refused", _reply(inbound, _pair_request_message(code)))
        reply = _reply(inbound, PAIR_HINT) if decision.should_reply else None
        return IngressResult("refused", reply)

    # 3) Admitted → run the ask control loop and format the reply.
    outcome = run_ask(conn, advisor, inbound.text, user_id=decision.user_id)

    # A complex job (or an escalated ask) is **planned** synchronously but runs in
    # the background: enqueue it so the job worker executes it and delivers the
    # result back to this chat later (quoting this message). The immediate reply
    # just acknowledges the /req (see `_format_outcome`).
    if outcome.status == "planned" and outcome.job_id is not None:
        job_queue_repo.enqueue(
            conn,
            outcome.job_id,
            channel=inbound.channel,
            chat_id=inbound.chat_id,
            reply_to_message_id=inbound.message_id,
            user_id=decision.user_id,
        )

    return IngressResult(
        "answered", _reply(inbound, _format_outcome(outcome)), user_id=decision.user_id
    )
