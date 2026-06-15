"""The PM / Gateway — first-pass request routing (design-spec §6A, §6C; T4.3).

The PM is the **only** role that talks to the user. On intake it gives every
message a home and auto-assigns a `/req <id>` (§6C, Stage 1 — fast first-pass):

  1. **Explicit address** (`/req <code> <message>`) → append to that request.
  2. **Empty queue** → the message can only be new → mint a new request.
  3. **Open requests, no marker** → best-guess append-vs-new.

The guess is provisional — the **Analyzer** authoritatively re-checks it (§6C,
Stage 2). The PM never blocks. It emits a `route_request` envelope to the Boss.

> P4 keeps the best-guess deterministic (mint-new on no explicit address) and the
> title a truncation of the message; the AI-assisted `pm.route` best-guess + title
> (semantic similarity) layer on later without changing this control path.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime

from app.roles.envelope import Action, Role, RoleMessage
from app.storage.repos import requests as requests_repo
from app.storage.repos.requests import Request

# `/req <code> <message>` — explicit address (deterministic, no AI).
_REQ_RE = re.compile(r"^/req\s+(?P<code>\S+)\s*(?P<rest>.*)$", re.DOTALL)

_MAX_TITLE_LEN = 60


def _draft_title(text: str) -> str:
    """A deterministic placeholder title from the first line (§6C; AI later)."""
    first_line = text.strip().splitlines()[0].strip() if text.strip() else ""
    if not first_line:
        return "untitled request"
    return first_line[:_MAX_TITLE_LEN].rstrip()


@dataclass(frozen=True)
class RouteResult:
    """The outcome of first-pass routing: where the message landed + the envelope."""

    request: Request
    append: bool
    text: str  # the effective message (rest after `/req <code>`, or the full input)
    card: dict  # the §6D RequestCard payload (also carried on `envelope.payload`)
    detail_id: int | None  # the appended `request_details` row, if any
    envelope: RoleMessage  # the `route_request` hand-off to the Boss (not yet persisted)


def _build_card(request: Request, *, text: str, append: bool) -> dict:
    """The §6D `RequestCard` payload the Analyzer consumes."""
    return {
        "request_id": request.id,
        "request_code": request.code,
        "title": request.title,
        "text": text,
        "append": append,
    }


def _route_request_envelope(request: Request, card: dict) -> RoleMessage:
    return RoleMessage(
        request_id=request.id,
        from_role=Role.pm,
        to_role=Role.boss,
        action=Action.route_request,
        payload=card,
    )


def route_inbound(
    conn,
    text: str,
    *,
    user_id: int | None = None,
    session_id: int | None = None,
    now: datetime | None = None,
) -> RouteResult:
    """Assign an inbound message to a request and emit `route_request` (§6C)."""
    match = _REQ_RE.match(text.strip())
    if match:
        code = match.group("code")
        rest = match.group("rest").strip()
        existing = requests_repo.get_request_by_code(conn, code)
        if existing is not None and rest:
            # Explicit append to an existing request (deterministic).
            detail_id = requests_repo.add_request_detail(
                conn, request_id=existing.id, content=rest, source="user", routed_by="pm"
            )
            card = _build_card(existing, text=rest, append=True)
            return RouteResult(
                request=existing,
                append=True,
                text=rest,
                card=card,
                detail_id=detail_id,
                envelope=_route_request_envelope(existing, card),
            )
        # Unknown code (or no message) → fall through and mint a new request
        # from the full original text; the user can re-address if needed.

    # Empty queue or no explicit address → mint a new request (P4 best-guess).
    request = requests_repo.create_request(
        conn,
        title=_draft_title(text),
        user_id=user_id,
        session_id=session_id,
        now=now,
    )
    card = _build_card(request, text=text, append=False)
    return RouteResult(
        request=request,
        append=False,
        text=text,
        card=card,
        detail_id=None,
        envelope=_route_request_envelope(request, card),
    )


def format_delivery(request: Request, answer_text: str) -> str:
    """Render the user-facing delivery, tagged with `/req <id>` + title (§6C)."""
    title = request.title or "untitled request"
    return f"/req {request.code} «{title}»\n\n{answer_text}"
