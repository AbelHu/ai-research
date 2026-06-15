"""Tests for the Boss job scheduler (implementation-plan T6.4).

Async coroutines are driven via ``asyncio.run`` (no pytest-asyncio dependency).
"""

from __future__ import annotations

import asyncio

import pytest

from app.roles.scheduler import JobScheduler


def test_caps_concurrency_three_run_one_queued() -> None:
    async def body() -> None:
        scheduler = JobScheduler(max_concurrent=3)
        release = asyncio.Event()
        started = {jid: asyncio.Event() for jid in (1, 2, 3, 4)}

        def make_runner(jid: int):
            async def runner() -> None:
                started[jid].set()
                await release.wait()

            return runner

        for jid in (1, 2, 3, 4):
            scheduler.submit(jid, make_runner(jid))

        # Let the first three acquire slots; the fourth waits on the cap.
        await asyncio.wait_for(
            asyncio.gather(started[1].wait(), started[2].wait(), started[3].wait()),
            timeout=1,
        )
        await asyncio.sleep(0)  # give the 4th a chance to (not) start

        assert scheduler.active_count == 3
        assert len(scheduler.running) == 3
        assert scheduler.queued == [4]
        assert not started[4].is_set()  # 4th is blocked on the slot gate

        release.set()
        await asyncio.wait_for(scheduler.join(), timeout=1)

        assert scheduler.active_count == 0
        assert scheduler.queued == []
        assert started[4].is_set()  # 4th ran once a slot freed

    asyncio.run(body())


def test_queued_job_runs_after_a_slot_frees() -> None:
    async def body() -> None:
        scheduler = JobScheduler(max_concurrent=1)
        order: list[int] = []
        gate1 = asyncio.Event()

        async def first() -> None:
            order.append(1)
            await gate1.wait()

        async def second() -> None:
            order.append(2)

        scheduler.submit(1, first)
        scheduler.submit(2, second)
        await asyncio.sleep(0)

        assert order == [1]  # only the first holds the single slot
        assert scheduler.queued == [2]

        gate1.set()  # free the slot → the queued job runs
        await asyncio.wait_for(scheduler.join(), timeout=1)
        assert order == [1, 2]

    asyncio.run(body())


def test_dispose_cancels_a_running_job() -> None:
    async def body() -> None:
        scheduler = JobScheduler(max_concurrent=2)
        started = asyncio.Event()

        async def forever() -> None:
            started.set()
            await asyncio.Event().wait()  # never completes on its own

        scheduler.submit(7, forever)
        await asyncio.wait_for(started.wait(), timeout=1)
        assert 7 in scheduler.running

        scheduler.dispose(7)
        await asyncio.wait_for(scheduler.join(), timeout=1)
        assert 7 not in scheduler.running

    asyncio.run(body())


def test_duplicate_submit_rejected() -> None:
    async def body() -> None:
        scheduler = JobScheduler(max_concurrent=2)

        async def noop() -> None:
            return None

        scheduler.submit(1, noop)
        with pytest.raises(ValueError, match="already scheduled"):
            scheduler.submit(1, noop)
        await scheduler.join()

    asyncio.run(body())


def test_default_cap_from_policies() -> None:
    async def body() -> None:
        scheduler = JobScheduler()
        assert scheduler.max_concurrent == 3  # policies.yaml default

    asyncio.run(body())
