"""Frozen review cycles and bounded off-turn work, owned by the game controller."""

import asyncio
from typing import Literal

from pydantic import model_validator

from civ_agent.orchestration.contracts import Digest, Reference, ReviewRequest, Scope
from civ_agent.schemas import BoundaryModel, NonnegativeInt


class ReviewCycle(BoundaryModel):
    version: Literal[1] = 1
    scope: Scope
    source_turn: NonnegativeInt
    target_turn: NonnegativeInt
    kind: Literal["opening", "periodic", "early"]
    inputs: Reference | None = None
    status: Literal["PENDING", "READY", "UNAVAILABLE"] = "PENDING"
    request: ReviewRequest | None = None
    committed: bool = True
    source_handoff: NonnegativeInt | None = None

    @property
    def id(self) -> Digest:
        return self.scope.activation

    @model_validator(mode="after")
    def target(self):
        if self.target_turn != (0 if self.kind == "opening" else self.source_turn + 1):
            raise ValueError("REVIEW_CYCLE_TARGET_CONFLICT")
        if self.kind == "opening" and self.source_turn != 0:
            raise ValueError("REVIEW_CYCLE_OPENING_CONFLICT")
        if self.status == "PENDING" and self.inputs is None:
            raise ValueError("REVIEW_CYCLE_INPUT_REQUIRED")
        return self


class ReviewScheduler:
    """Give the next player priority without consuming its three role slots."""

    def __init__(self, capacity: int):
        self.background_slots = max(0, capacity - 3)
        self.required: tuple[str, int] | None = None
        self.running: set[tuple[str, int]] = set()
        self.tasks: dict[tuple[str, int], asyncio.Task] = {}
        self.condition = asyncio.Condition()
        self.failures: list[BaseException] = []

    async def prioritize(self, player: str, turn: int):
        async with self.condition:
            self.required = (player, turn)
            self.condition.notify_all()

    async def _acquire(self, key):
        async with self.condition:
            await self.condition.wait_for(
                lambda: (
                    key == self.required
                    or sum(other != self.required for other in self.running) < self.background_slots
                )
            )
            self.running.add(key)

    async def submit(self, player, cycle: ReviewCycle):
        key = (cycle.scope.player, cycle.target_turn)
        if cycle.status != "PENDING" or not cycle.committed:
            return None
        if key in self.tasks:
            return self.tasks[key]

        async def work():
            await self._acquire(key)
            try:
                await player.review(cycle)
            finally:
                async with self.condition:
                    self.running.discard(key)
                    self.condition.notify_all()

        task = self.tasks[key] = asyncio.create_task(work(), name="review:" + cycle.id)

        def settled(done):
            if not done.cancelled() and (error := done.exception()) is not None:
                if not self.failures:
                    self.failures.append(error)
            if self.tasks.get(key) is done:
                self.tasks.pop(key)

        task.add_done_callback(settled)
        return task

    async def wait(self, player, turn: int):
        await self.prioritize(player.session.player, turn)
        service = getattr(player.root, "reviews", None)
        if service is None:
            return
        cycle = await service.for_target(turn)
        if cycle is not None and cycle.status == "PENDING":
            task = await self.submit(player, cycle)
            if task is not None:
                await task
        await service.require_target(turn)

    def raise_if_failed(self):
        for task in list(self.tasks.values()):
            if task.done() and not task.cancelled() and (error := task.exception()) is not None:
                raise error
        if self.failures:
            raise self.failures[0]

    async def close(self):
        for task in self.tasks.values():
            if not task.done() and not task.cancelling():
                task.cancel()
        await asyncio.gather(*list(self.tasks.values()), return_exceptions=True)
