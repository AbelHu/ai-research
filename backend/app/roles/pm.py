"""The PM / Gateway — first-pass request routing (design-spec §6A, §6C; T4.3).

The PM is the **only** role that talks to the user. On intake it gives every
message a home and auto-assigns a `/req <id>` (§6C, Stage 1 — fast first-pass):

  1. **Explicit address** (`/req <code> <message>`) → append to that request.
  2. **No marker, but the sender has a request awaiting their reply** (we asked
     for clarification, or a plan was declined) → thread the follow-up back to it.
  3. **No marker, but the sender has a current thread** → **provisionally** append
     to it (best-guess) without persisting yet; the Analyzer confirms `belongs`.
  4. **Otherwise** (no open thread) → mint a new request.

The provisional guess (3) is re-checked by the **Analyzer** (§6C, Stage 2): on
confirmation the control loop persists the appended detail; on rejection it mints
a fresh request. The PM never blocks. It emits a `route_request` envelope.

> P4 keeps the best-guess deterministic (mint-new on no explicit address) and the
> title a truncation of the message; the AI-assisted `pm.route` best-guess + title
> (semantic similarity) layer on later without changing this control path.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime

from app.advisor.schemas import Source
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
    provisional: bool = False  # a best-guess append the Analyzer must still confirm (§6C)


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


def _append_result(conn, existing: Request, text: str) -> RouteResult:
    """Append ``text`` to ``existing`` and build its ``route_request`` hand-off."""
    detail_id = requests_repo.add_request_detail(
        conn, request_id=existing.id, content=text, source="user", routed_by="pm"
    )
    card = _build_card(existing, text=text, append=True)
    return RouteResult(
        request=existing,
        append=True,
        text=text,
        card=card,
        detail_id=detail_id,
        envelope=_route_request_envelope(existing, card),
    )


def route_new(
    conn,
    text: str,
    *,
    user_id: int | None = None,
    session_id: int | None = None,
    now: datetime | None = None,
) -> RouteResult:
    """Mint a brand-new request for ``text`` (the no-open-thread / undo path)."""
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
            return _append_result(conn, existing, rest)
        # Unknown code (or no message) → fall through and mint a new request
        # from the full original text; the user can re-address if needed.

    if user_id is not None:
        # A request **awaiting the user's reply** (we asked for clarification, or
        # a plan was declined) → thread the follow-up back to it deterministically
        # and hand the turn back (§6C). This is a definite continuation.
        awaiting = requests_repo.get_latest_awaiting_request(conn, user_id)
        if awaiting is not None:
            requests_repo.set_request_status(conn, awaiting.id, None)
            return _append_result(conn, awaiting, text)

        # Otherwise best-guess that the message continues the user's current
        # thread (§6C, Stage 1). The guess is **provisional**: the detail is NOT
        # persisted yet — the Analyzer authoritatively judges `belongs` (Stage 2),
        # and the control loop persists it on confirmation or mints a fresh
        # request on rejection. This keeps references resolvable without
        # committing a possibly-wrong association.
        latest = requests_repo.get_latest_active_request(conn, user_id)
        if latest is not None:
            card = _build_card(latest, text=text, append=True)
            return RouteResult(
                request=latest,
                append=True,
                text=text,
                card=card,
                detail_id=None,
                provisional=True,
                envelope=_route_request_envelope(latest, card),
            )

    # No open thread for this user → mint a new request.
    return route_new(conn, text, user_id=user_id, session_id=session_id, now=now)


def format_delivery(
    request: Request, answer_text: str, *, sources: list[Source] | None = None
) -> str:
    """Render the user-facing delivery, tagged with `/req <id>` + title (§6C).

    When the answer carries sources, list them beneath it so the user actually
    sees the provenance — the memory ref or source URL — backing the answer
    (§7.1). An answer with no sources is delivered as the bare answer text.
    """
    title = request.title or "untitled request"
    body = f"/req {request.code} «{title}»\n\n{answer_text}"
    source_lines = _format_sources(sources or [])
    if source_lines:
        body += "\n\n" + "\n".join(source_lines)
    return body


def _format_sources(sources: list[Source]) -> list[str]:
    """Render citations as a human-readable `Sources:` block (one bullet each).

    Prefers a titled link (`title — url`), falls back to a bare URL, and finally
    to the opaque `ref` for a non-URL source (e.g. a memory citation).
    """
    if not sources:
        return []
    lines = ["Sources:"]
    for source in sources:
        if source.url and source.title:
            lines.append(f"  - {source.title} — {source.url}")
        elif source.url:
            lines.append(f"  - {source.url}")
        elif source.title:
            lines.append(f"  - {source.title} ({source.ref})")
        else:
            lines.append(f"  - {source.ref}")
    return lines
