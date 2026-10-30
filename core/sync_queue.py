from __future__ import annotations
import asyncio
import logging
from typing import Callable, Awaitable

log = logging.getLogger(__name__)

# Seconds to wait after the last enqueue before flushing (debounce)
_DEBOUNCE = 2.0


class SyncQueue:
    """
    Debounced asyncio queue for template sync work.

    Callers enqueue template_ids.  After DEBOUNCE seconds of silence the
    worker drains the queue, deduplicates, and calls the registered handler
    once per unique template_id.
    """

    def __init__(self) -> None:
        self._queue: asyncio.Queue[int] = asyncio.Queue()
        self._handler: Callable[[int], Awaitable[None]] | None = None
        self._task: asyncio.Task | None = None

    def set_handler(self, handler: Callable[[int], Awaitable[None]]) -> None:
        self._handler = handler

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._worker(), name="sync-queue-worker")

    def stop(self) -> None:
        if self._task and not self._task.done():
            self._task.cancel()

    async def enqueue(self, template_id: int) -> None:
        await self._queue.put(template_id)

    async def _worker(self) -> None:
        while True:
            # Block until there is at least one item
            first = await self._queue.get()
            pending: set[int] = {first}

            # Debounce: keep draining until DEBOUNCE seconds of silence
            while True:
                try:
                    item = await asyncio.wait_for(self._queue.get(), timeout=_DEBOUNCE)
                    pending.add(item)
                except asyncio.TimeoutError:
                    break

            if self._handler is None:
                log.warning("SyncQueue has no handler set; dropping %d item(s)", len(pending))
                continue

            for template_id in pending:
                try:
                    await self._handler(template_id)
                except Exception:
                    log.exception("SyncQueue handler raised for template_id=%d", template_id)
