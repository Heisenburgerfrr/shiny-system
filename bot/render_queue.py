"""
Asynchronous FFmpeg Render Queue for serializing heavy video processing tasks.
Guarantees that at most one FFmpeg process runs at any time to prevent CPU lockups,
memory depletion, and server crashes on constrained cloud VMs.
"""

import asyncio
import logging
from contextlib import asynccontextmanager
from typing import AsyncGenerator, Awaitable, Callable, List, Optional

logger = logging.getLogger("bot.render_queue")


class AsyncRenderQueue:
    """
    Manages access to the FFmpeg rendering pipeline with a concurrency limit of 1.
    Maintains an ordered queue of waiting jobs and notifies callbacks of queue position.
    """

    def __init__(self, max_concurrent_renders: int = 1):
        self._semaphore = asyncio.Semaphore(max_concurrent_renders)
        self._waiting_jobs: List[str] = []
        self._active_job: Optional[str] = None
        self._lock = asyncio.Lock()  # Protects internal queue state modifications

    @property
    def is_busy(self) -> bool:
        """Returns True if a render job is currently active."""
        return self._active_job is not None or self._semaphore.locked()

    @property
    def queue_length(self) -> int:
        """Returns the number of jobs waiting in the queue."""
        return len(self._waiting_jobs)

    def get_position(self, job_id: str) -> Optional[int]:
        """Returns 1-based queue position of job_id, or None if not waiting."""
        try:
            return self._waiting_jobs.index(job_id) + 1
        except ValueError:
            return None

    @asynccontextmanager
    async def acquire_slot(
        self,
        job_id: str,
        on_wait_callback: Optional[Callable[[int], Awaitable[None]]] = None,
    ) -> AsyncGenerator[None, None]:
        """
        Acquires an exclusive rendering slot. If another job is currently rendering,
        registers job_id in the queue, calls on_wait_callback with the queue position,
        and awaits its turn.
        """
        needs_wait = False
        queue_pos = 0

        async with self._lock:
            if self.is_busy:
                needs_wait = True
                if job_id not in self._waiting_jobs:
                    self._waiting_jobs.append(job_id)
                queue_pos = len(self._waiting_jobs)
                logger.info(
                    "[%s] Render pipeline busy (active=%s). Job queued at position #%d (total waiting=%d).",
                    job_id,
                    self._active_job,
                    queue_pos,
                    len(self._waiting_jobs),
                )

        if needs_wait and on_wait_callback:
            try:
                await on_wait_callback(queue_pos)
            except Exception as cb_exc:
                logger.debug("[%s] Failed to invoke render wait callback: %s", job_id, cb_exc)

        # Await exclusive semaphore slot
        await self._semaphore.acquire()

        async with self._lock:
            if job_id in self._waiting_jobs:
                self._waiting_jobs.remove(job_id)
            self._active_job = job_id
            logger.info(
                "[%s] Acquired exclusive render slot. Starting video processing. Remaining in queue: %d.",
                job_id,
                len(self._waiting_jobs),
            )

        try:
            yield
        finally:
            async with self._lock:
                self._active_job = None
                self._semaphore.release()
                logger.info(
                    "[%s] Released render slot. Available for next queued task.",
                    job_id,
                )


# Global singleton instance for the bot application
render_queue = AsyncRenderQueue(max_concurrent_renders=1)
