"""Public StepStore writes with bounded, process-local invalidation tokens.

Tokens invalidate derived views around write attempts; they do not prove that a
write committed or describe recoverable state. Authoritative reads delegate to the SDK; a new wrapper/recovery starts with an empty index. No SDK SQL is used.
"""

import asyncio
import threading
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from uuid import uuid4

from pydantic_ai_harness.step_persistence import StepStore


class CommittedStepStore:
    def __init__(self, wrapped: StepStore, *, max_runs: int = 256):
        if max_runs < 1:
            raise ValueError("STEP_NOTIFICATION_LIMIT_REQUIRED")
        self.wrapped = wrapped
        self.max_runs = max_runs
        self.identity = uuid4().hex
        self._sequence = 0
        self._runs: OrderedDict[str, int] = OrderedDict()
        self._pending: dict[str, int] = {}
        self._listeners: set[Callable[[str], None]] = set()
        self._lock = threading.RLock()

    def subscribe(self, listener: Callable[[str], None]) -> Callable[[], None]:
        with self._lock:
            self._listeners.add(listener)

        def unsubscribe():
            with self._lock:
                self._listeners.discard(listener)

        return unsubscribe

    def snapshot_token(self, run_id: str) -> tuple[str, str, int] | None:
        """A missing/evicted run conservatively changes after any store write."""
        with self._lock:
            if self._pending.get(run_id):
                return None
            if run_id in self._runs:
                return self.identity, "run", self._runs[run_id]
            return self.identity, "unindexed", self._sequence

    def _invalidate(self, run_id: str, *, entering: bool):
        with self._lock:
            self._sequence += 1
            self._runs[run_id] = self._sequence
            self._runs.move_to_end(run_id)
            while len(self._runs) > self.max_runs:
                self._runs.popitem(last=False)
            if entering:
                self._pending[run_id] = self._pending.get(run_id, 0) + 1
            else:
                remaining = self._pending[run_id] - 1
                if remaining:
                    self._pending[run_id] = remaining
                else:
                    del self._pending[run_id]
            listeners = tuple(self._listeners)
        for listener in listeners:
            try:
                listener(run_id)
            except Exception:
                # Presentation must not change an SDK write's outcome.
                pass

    async def _write(self, run_id: str, operation: Awaitable[None]):
        self._invalidate(run_id, entering=True)
        try:
            # The SDK may be awaiting an uncancellable SQLite worker thread.
            # Drain an accepted public write before exposing a stable token;
            # cancelling our caller must not leave a later commit unnoticed.
            pending = asyncio.ensure_future(operation)
            cancelled = False
            while not pending.done():
                try:
                    await asyncio.shield(pending)
                except asyncio.CancelledError:
                    cancelled = True
            result = pending.result()
            if cancelled:
                raise asyncio.CancelledError
            return result
        finally:
            self._invalidate(run_id, entering=False)

    async def register_run(self, record):
        return await self._write(record.run_id, self.wrapped.register_run(record))

    async def save_snapshot(self, snapshot):
        return await self._write(snapshot.run_id, self.wrapped.save_snapshot(snapshot))

    async def get_run(self, **kwargs):
        return await self.wrapped.get_run(**kwargs)

    async def list_runs(self, **kwargs):
        return await self.wrapped.list_runs(**kwargs)

    async def append_event(self, event):
        return await self.wrapped.append_event(event)

    async def list_events(self, **kwargs):
        return await self.wrapped.list_events(**kwargs)

    async def latest_snapshot(self, **kwargs):
        return await self.wrapped.latest_snapshot(**kwargs)

    async def record_tool_effect(self, record):
        return await self.wrapped.record_tool_effect(record)

    async def get_tool_effect(self, **kwargs):
        return await self.wrapped.get_tool_effect(**kwargs)

    async def list_unresolved_tool_effects(self, **kwargs):
        return await self.wrapped.list_unresolved_tool_effects(**kwargs)

    def __getattr__(self, name):
        return getattr(self.wrapped, name)
