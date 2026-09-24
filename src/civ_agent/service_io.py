"""Ordered service I/O which never runs on the HTTPS event loop."""

import asyncio
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from functools import partial


class ServiceIO:
    def __init__(self) -> None:
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="agent-history")
        self.lock = asyncio.Lock()
        self.closed = False
        self.slow_callback: Callable[..., None] | None = None

    async def call[**P, T](self, operation: Callable[P, T], *args: P.args, **kwargs: P.kwargs) -> T:
        # Submit at most one operation at a time. Awaiting callers provide
        # backpressure instead of accumulating an unbounded executor queue.
        queued = time.monotonic()
        async with self.lock:
            if self.closed:
                raise RuntimeError("Service I/O is closed")
            future = asyncio.get_running_loop().run_in_executor(self.executor, partial(operation, *args, **kwargs))
            started = time.monotonic()
            cancelled = False
            try:
                while True:
                    try:
                        result = await asyncio.shield(future)
                        break
                    except asyncio.CancelledError:
                        # Even repeated interrupts must finish an accepted append
                        # before terminal flush, another writer, or shutdown.
                        cancelled = True
            finally:
                finished = time.monotonic()
                if self.slow_callback is not None and finished - queued >= 1:
                    self.slow_callback(
                        "service_io_slow",
                        operation=getattr(operation, "__qualname__", type(operation).__name__),
                        queueMs=round((started - queued) * 1000),
                        workMs=round((finished - started) * 1000),
                    )
            if cancelled:
                raise asyncio.CancelledError
            return result

    async def close(self) -> None:
        async with self.lock:
            if not self.closed:
                self.closed = True
                self.executor.shutdown(wait=True)
