"""Deadline-bound retrieval reads, including transports that take time to close."""
import asyncio
from threading import BoundedSemaphore


async def run_until(end, call, *args, **kwargs):
    loop = asyncio.get_running_loop()
    if loop.time() >= end:
        raise TimeoutError
    async with asyncio.timeout_at(end) as timer:
        result = await call(*args, **kwargs)
    if timer.expired() or loop.time() >= end:
        raise TimeoutError
    return result


class ReadTasks:
    def __init__(self, capacity):
        self.slots = BoundedSemaphore(capacity)
        self.active = set()

    def start(self, call, *args, **kwargs):
        if not self.slots.acquire(blocking=False):
            raise BlockingIOError('Retrieval read capacity exhausted')
        async def execute():
            try:
                return await call(*args, **kwargs)
            finally:
                self._release(asyncio.current_task())

        work = execute()
        try:
            task = asyncio.create_task(work)
        except BaseException:
            work.close()
            self.slots.release()
            raise
        self.active.add(task)
        if task.done():
            self._finished(task)
        else:
            task.add_done_callback(self._finished)
        return task

    def _release(self, task):
        if task in self.active:
            self.active.remove(task)
            self.slots.release()

    def _finished(self, task):
        # Cancellation before the first coroutine step bypasses its finally block.
        self._release(task)
        if not task.cancelled():
            task.exception()

    async def until(self, end, call, *args, **kwargs):
        loop = asyncio.get_running_loop()

        async def read():
            if loop.time() >= end:
                raise TimeoutError
            return await call(*args, **kwargs)

        if loop.time() >= end:
            raise TimeoutError
        task = self.start(read)
        try:
            return await run_until(end, asyncio.shield, task)
        except asyncio.CancelledError:
            if task.cancelled() and not asyncio.current_task().cancelling():
                raise ConnectionError('Retrieval backend cancelled its read') from None
            raise
        finally:
            if not task.done():
                task.cancel()


# Shared by requests within one backend process. Cancelled reads retain their
# capacity until the actual coroutine exits, independently of the caller's SLA.
retrieval_reads = ReadTasks(64)
