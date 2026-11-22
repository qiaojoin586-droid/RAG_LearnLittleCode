"""Bound outstanding synchronous work, including calls that outlive a reader."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from contextvars import copy_context
from threading import BoundedSemaphore, Event
from anyio import CancelScope


async def finish_before_cancelling(work):
    """A completed write must be acknowledged before releasing its guard."""
    try:
        return await asyncio.shield(work)
    except asyncio.CancelledError:
        with CancelScope(shield=True):
            while not work.done():
                try:
                    await asyncio.shield(work)
                except asyncio.CancelledError:
                    continue
                except Exception:
                    break
        if not work.cancelled():
            work.exception()
        raise


class BlockingPool:
    def __init__(self, name, workers, queued):
        self.executor = ThreadPoolExecutor(max_workers=workers, thread_name_prefix=name)
        self.slots = BoundedSemaphore(workers + queued)

    async def run(self, operation, *, finish_on_cancel=False):
        if not self.slots.acquire(blocking=False):
            raise BlockingIOError('Retrieval worker capacity exhausted')
        abandoned = Event()
        def run_if_needed():
            if not abandoned.is_set():
                return operation()
        try:
            future = self.executor.submit(copy_context().run, run_if_needed)
        except BaseException:
            self.slots.release()
            raise
        future.add_done_callback(lambda _: self.slots.release())
        work = asyncio.wrap_future(future)
        if finish_on_cancel:
            return await finish_before_cancelling(work)
        try:
            return await asyncio.shield(work)
        except asyncio.CancelledError:
            abandoned.set()
            work.add_done_callback(lambda done: None if done.cancelled() else done.exception())
            raise
