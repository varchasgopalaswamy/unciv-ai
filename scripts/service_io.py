"""Ordered service I/O which never runs on the HTTPS event loop."""

import asyncio
from concurrent.futures import ThreadPoolExecutor
from functools import partial


class ServiceIO:
    def __init__(self):
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="agent-history")
        self.lock = asyncio.Lock()
        self.closed = False

    async def call(self, operation, *args, **kwargs):
        # Submit at most one operation at a time. Awaiting callers provide
        # backpressure instead of accumulating an unbounded executor queue.
        async with self.lock:
            if self.closed:
                raise RuntimeError("Service I/O is closed")
            future = asyncio.get_running_loop().run_in_executor(self.executor, partial(operation, *args, **kwargs))
            cancelled = False
            while True:
                try:
                    result = await asyncio.shield(future)
                    break
                except asyncio.CancelledError:
                    # Even repeated interrupts must finish an accepted append
                    # before terminal flush, another writer, or shutdown.
                    cancelled = True
            if cancelled:
                raise asyncio.CancelledError
            return result

    async def close(self):
        async with self.lock:
            if not self.closed:
                self.closed = True
                self.executor.shutdown(wait=True)
