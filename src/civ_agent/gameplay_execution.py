"""Evaluation-only graceful stops at native turn boundaries."""

import asyncio
from collections.abc import Callable

from civ_agent.runtime.controller import LocalController


class EvaluationController(LocalController):
    """A requested cohort stop finishes the current turn without revoking its lease.

    Background communication and reviews settle before the normal controller
    barrier/cleanup. In particular, pausing the native host must not cancel a
    still-admitted provider stream through its heartbeat failure.
    """

    def __init__(self, *args, stop_requested: Callable[[], bool], on_recovered=None, on_prepared=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.stop_requested = stop_requested
        self.on_recovered = on_recovered
        self.on_prepared = on_prepared

    async def _prepare_reviews(self):
        if self.on_prepared is not None:
            self.on_prepared(self.store.current)
        await super()._prepare_reviews()

    async def _recover(self, *, terminal=False):
        await super()._recover(terminal=terminal)
        if not terminal and self.on_recovered is not None:
            await self.on_recovered(self)

    def _limit(self):
        return "EVALUATION_COHORT_STOP" if self.stop_requested() else super()._limit()

    async def _shutdown(self):
        try:
            state = self.store.state
            if state is not None and state.mode in {"STOPPED", "FINISHED"}:
                # Keep the heartbeat and native authority alive during this drain.
                # This blocks new communication jobs and joins existing ones.
                await self.communications(True)
                results = await asyncio.gather(*list(self.reviews.tasks.values()), return_exceptions=True)
                failures = [result for result in results if isinstance(result, BaseException)]
                if failures:
                    state.mode, state.phase, state.reason = "FAILED", "FAILED", "EVALUATION_DRAIN_FAILED"
                    await self.store.save()
        finally:
            await super()._shutdown()
