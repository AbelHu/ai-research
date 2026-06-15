"""Boss job scheduler — concurrency-capped per-job runners (§6A/§6B; T6.4).

The Boss starts a per-job runner as an in-process **`asyncio` task** (Open
decision #2) and caps how many run at once at ``max_concurrent_jobs`` (default
3); extra jobs **queue** until a slot frees. Isolation is logical — each runner
owns its own coroutine + (later) `JobContext` + folder + inbox; there is **no
separate OS process**.

The scheduler is generic in the *runner coroutine* so it can be tested without
real job work: ``submit(job_id, runner)`` admits a job through the slot gate and
runs ``runner()`` when a slot is available. ``running``/``queued`` expose live
admission state for status (`/req`) and tests.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable

from app.config.policies import get_policies

# A job runner is a no-arg coroutine factory (the work to run under one slot).
JobRunner = Callable[[], Awaitable[None]]


class JobScheduler:
    """Admit at most ``max_concurrent`` job runners at once; queue the rest."""

    def __init__(self, *, max_concurrent: int | None = None) -> None:
        self.max_concurrent = (
            max_concurrent if max_concurrent is not None else get_policies().max_concurrent_jobs
        )
        self._sem = asyncio.Semaphore(self.max_concurrent)
        self.running: set[int] = set()
        self.queued: list[int] = []
        self._tasks: dict[int, asyncio.Task] = {}

    def submit(self, job_id: int, runner: JobRunner) -> asyncio.Task:
        """Schedule ``runner`` for ``job_id`` under the slot gate; return its task.

        The job is marked **queued** immediately and becomes **running** only
        once it acquires a slot — so ``queued`` reflects jobs waiting on the cap.
        """
        if job_id in self._tasks:
            raise ValueError(f"job {job_id} already scheduled")
        self.queued.append(job_id)
        task = asyncio.ensure_future(self._run_with_slot(job_id, runner))
        self._tasks[job_id] = task
        return task

    async def _run_with_slot(self, job_id: int, runner: JobRunner) -> None:
        await self._sem.acquire()
        try:
            if job_id in self.queued:
                self.queued.remove(job_id)
            self.running.add(job_id)
            await runner()
        finally:
            self.running.discard(job_id)
            if job_id in self.queued:
                self.queued.remove(job_id)
            self._sem.release()

    def dispose(self, job_id: int) -> None:
        """Cancel a job's runner (the Boss disposes after archive/abandon).

        The task is **kept tracked** so a subsequent `join()` still awaits its
        cancellation handler (e.g. the abandon-on-cancel cleanup) to completion;
        the slot is freed by ``_run_with_slot``'s ``finally``.
        """
        task = self._tasks.get(job_id)
        if task is not None and not task.done():
            task.cancel()
        self.running.discard(job_id)
        if job_id in self.queued:
            self.queued.remove(job_id)

    async def join(self) -> None:
        """Await every scheduled runner to completion (or cancellation)."""
        if self._tasks:
            await asyncio.gather(*self._tasks.values(), return_exceptions=True)

    @property
    def active_count(self) -> int:
        return len(self.running)
