"""Tests for SkillContext (implementation-plan T2.2)."""

from __future__ import annotations

from app.skills.context import SkillContext
from app.storage.db import connect


def test_context_holds_deterministic_services() -> None:
    conn = connect()  # in-memory "fake db"
    try:
        ctx = SkillContext(
            user_id=1,
            conn=conn,
            permissions=frozenset({"memory.read"}),
            job_id=42,
            task_id=7,
        )
        assert ctx.user_id == 1
        assert ctx.conn is conn
        assert "memory.read" in ctx.permissions
        assert ctx.job_id == 42
        assert ctx.task_id == 7
        assert ctx.logger is not None
    finally:
        conn.close()


def test_context_has_no_model() -> None:
    conn = connect()
    try:
        ctx = SkillContext(user_id=1, conn=conn)
        # Skills are model-independent: there is no way to reach the AI (§8.5).
        for forbidden in ("model", "provider", "advisor", "ai", "complete"):
            assert not hasattr(ctx, forbidden)
        # Sensible defaults when omitted.
        assert ctx.permissions == frozenset()
        assert ctx.job_id is None
        assert ctx.task_id is None
    finally:
        conn.close()
