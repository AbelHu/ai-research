"""Improvement loop — finish, archive/close, optionally spawn (§6B; plan T6.8).

When a complex job finishes, the original is **archived + closed on both
branches** (confirm or decline). If the user confirms an improvement *and* the
improvement chain is under the ``max_improvement_iterations`` cap (default 2), a
**new linked improvement request** is spawned (`improves_request_id` /
`spawned_request_id`) to carry it out as its own request/job — never a forced
tail on every job. Code, not the model, enforces "close first, then spawn" and
the iteration cap.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.config.policies import get_policies
from app.roles.envelope import Role
from app.storage.repos import library as library_repo
from app.storage.repos import plans as plans_repo
from app.storage.repos import requests as requests_repo
from app.storage.repos.requests import Request


@dataclass(frozen=True)
class FinishResult:
    closed: bool
    spawned_request: Request | None
    capped: bool  # True if an improvement was confirmed but blocked by the cap


def improvement_chain_depth(conn, request_id: int) -> int:
    """How many ``improves_request_id`` links lead back to an origin (0 = origin)."""
    depth = 0
    current = requests_repo.get_request(conn, request_id)
    while current is not None and current.improves_request_id is not None:
        depth += 1
        current = requests_repo.get_request(conn, current.improves_request_id)
    return depth


def finish_job(
    conn,
    *,
    request_id: int,
    plan_id: int,
    confirm_improvement: bool,
    improvement_title: str | None = None,
    final_report_id: int | None = None,
    max_improvement_iterations: int | None = None,
) -> FinishResult:
    """Close + archive the original, then optionally spawn a linked improvement.

    The plan must be ``Resolved`` (Company Expert signed off the whole plan).
    On both branches the plan goes ``Resolved -> Closed`` (Librarian) and the
    request is archived **first**; only then, if confirmed and under the cap, is
    a new linked improvement request minted.
    """
    cap = (
        max_improvement_iterations
        if max_improvement_iterations is not None
        else get_policies().max_improvement_iterations
    )

    # 1) Archive + close the original on BOTH branches.
    plans_repo.set_plan_status(conn, plan_id, "Closed", actor=Role.librarian)
    requests_repo.set_request_state(conn, request_id, "archived")

    # 2) Optionally spawn a linked improvement (after closure), capped.
    spawned: Request | None = None
    capped = False
    if confirm_improvement:
        if improvement_chain_depth(conn, request_id) >= cap:
            capped = True
        else:
            origin = requests_repo.get_request(conn, request_id)
            title = improvement_title or f"Improve: {origin.title or origin.code}"
            spawned = requests_repo.create_request(
                conn,
                title=title,
                user_id=origin.user_id,
                improves_request_id=request_id,
            )

    if final_report_id is not None:
        library_repo.set_final_report_confirmation(
            conn,
            final_report_id,
            user_confirmed=confirm_improvement,
            spawned_request_id=spawned.id if spawned else None,
        )

    return FinishResult(closed=True, spawned_request=spawned, capped=capped)
