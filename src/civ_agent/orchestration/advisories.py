"""Instance-local advisory calls with broker-owned deduplication and spending."""

import asyncio
from contextvars import ContextVar
from dataclasses import dataclass

from civ_agent.advisory_policy import AdvisoryPolicy
from civ_agent.decisions_backends import BACKENDS
from civ_agent.decisions_policy import DecisionsSettings, decisions_values
from civ_agent.decisions_tasks import Evidence, acceptance_threshold, request_body, request_id, validate_result
from civ_agent.orchestration.advisory_activities import AdvisoryServices
from civ_agent.orchestration.contracts import (
    AdvisoryExchange,
    AdvisoryRequest,
    AdvisoryResult,
    FinishAdvisory,
    NativeAuxiliaryTask,
    Scope,
    digest,
)
from civ_agent.orchestration.store import JournalConflict


@dataclass(frozen=True)
class _LocalAdvisory:
    epoch: str
    generation: int


class NativeAdvisories(AdvisoryPolicy):
    def __init__(self, root, broker, settings):
        settings = DecisionsSettings.model_validate(decisions_values(settings))
        super().__init__(root.gateway, settings)
        self.root = root
        self.services = AdvisoryServices(root.store, broker, settings, civilization=root.civilization)
        self.pending: set[asyncio.Task] = set()
        self.suspended, self.generation = False, 0
        self.context: ContextVar[_LocalAdvisory | None] = ContextVar("native_advisory", default=None)

    def suspend(self):
        # Stop callbacks; the broker retains already reserved spend and immutable replies.
        self.suspended = True
        self.generation += 1
        for task in tuple(self.pending):
            task.cancel()

    async def drain(self):
        self.suspend()
        tasks = tuple(self.pending)
        if asyncio.current_task() in tasks:
            raise JournalConflict("ADVISORY_DRAIN_FROM_CALLBACK")
        joining = asyncio.gather(*tasks, return_exceptions=True)
        cancelled = False
        while not joining.done():
            try:
                await asyncio.shield(joining)
            except asyncio.CancelledError:
                cancelled = True
        joining.result()
        self.pending.difference_update(tasks)
        if cancelled:
            raise asyncio.CancelledError

    def resume(self):
        self.suspended = False

    def _check(self, context=None):
        if self.suspended or context is not None and context.generation != self.generation:
            raise asyncio.CancelledError

    async def _local(self, operation):
        context = self.context.get()
        self._check(context)
        if context is None:
            with self.root.memory.transaction() as db:
                epoch = self.root.memory._meta(db, "epoch")
            context = _LocalAdvisory(epoch, self.generation)

        async def invoke():
            token = self.context.set(context)
            try:
                result = await operation()
                self._check(context)
                return result
            finally:
                self.context.reset(token)

        task = asyncio.create_task(invoke())
        self.pending.add(task)
        try:
            return await task
        finally:
            self.pending.discard(task)

    async def save(self, **values):
        context = self.context.get()
        if context is None:
            return await self._local(lambda: self.save(**values))
        self._check(context)
        saved = await self.gateway.io.call(self.store.save, expected_epoch=context.epoch, **values)
        self._check(context)
        if not saved:
            raise JournalConflict("ADVISORY_EPOCH_CONFLICT")

    async def startup(self, job, reads, topics, *, resume_pending=True):
        return await self._local(
            lambda: super(NativeAdvisories, self).startup(job, reads, topics, resume_pending=resume_pending)
        )

    async def triage(self, turn, *, scheduled_soon, next_review_turn):
        return await self._local(
            lambda: super(NativeAdvisories, self).triage(
                turn, scheduled_soon=scheduled_soon, next_review_turn=next_review_turn
            )
        )

    async def review_scheduled(self, turn):
        return await self._local(lambda: super(NativeAdvisories, self).review_scheduled(turn))

    def _register(self, epoch, key, value):
        with self.root.memory.transaction() as db:
            if self.root.memory._meta(db, "epoch") != epoch:
                return False
            self.root.memory._set(db, key, value)
            return True

    async def evaluate(self, evidence, *, task: NativeAuxiliaryTask | None = None):
        return await self._local(lambda: self._evaluate(evidence, task=task))

    async def _evaluate(self, evidence, *, task: NativeAuxiliaryTask | None = None):
        root = self.root
        try:
            frozen = Evidence.model_validate(evidence).model_dump(exclude_none=True)
            body = request_body(frozen, self.settings)
            identifier = request_id(frozen, model=BACKENDS[self.settings.decisions_backend].model)
        except (ValueError, TypeError, KeyError):
            return {"answers": [], "rawAnswers": [], "questionResults": [], "fallback": "input_unavailable"}
        if frozen["player"] != root.civilization or not self.settings.decisions_enabled:
            raise JournalConflict("ADVISORY_PLAYER_CONFLICT")
        context = self.context.get()
        assert context is not None
        scope = Scope(
            game=root.game,
            player=root.player,
            epoch=context.epoch,
            activation=digest(
                ["advisory", frozen, self.settings.model_dump(), task.model_dump()]
                if task is not None
                else ["advisory", frozen, self.settings.model_dump()]
            ),
        )
        request = AdvisoryRequest(
            scope=scope,
            evidence=root.store.put(scope, frozen),
            policy=root.store.put(scope, self.settings.model_dump()),
            request_id=identifier,
            timeout_seconds=self.settings.decisions_timeout_seconds,
            task=task,
        )
        if not await self.gateway.io.call(
            self._register, scope.epoch, "nativeAdvisory:" + scope.activation, request.model_dump(mode="json")
        ):
            raise JournalConflict("ADVISORY_EPOCH_CONFLICT")
        self._check(context)
        reply = await self.services.exchange(AdvisoryExchange(request=request))
        reconciled = reply.unresolved
        if reconciled:
            reply = await self.services.exchange(AdvisoryExchange(request=request, reconcile=True))
        result = await self.services.finish(FinishAdvisory(request=request, reply=reply, reconciled=reconciled))
        self._check(context)
        if (
            result.scope != scope
            or result.request_id != identifier
            or root.store.receipt(scope, "advisory-result") != result.model_dump(mode="json")
        ):
            raise JournalConflict("ADVISORY_RESULT_BINDING_CONFLICT")
        supplied = root.store.get(scope, result.outcome)
        validated = validate_result(body, frozen, supplied, acceptance_threshold(frozen, self.settings))
        if supplied.get("brokerReconciliation"):
            validated["brokerReconciliation"] = True
        self._check(context)
        saved = await self.gateway.io.call(self.store.save_attempt, scope.epoch, identifier, validated)
        self._check(context)
        if not saved:
            raise JournalConflict("ADVISORY_EPOCH_CONFLICT")
        return validated

    async def results(self, scope: Scope) -> list[AdvisoryResult]:
        from sqlalchemy import select

        from civ_agent import memory_schema as tables

        root = self.root
        root.check_owner(scope)
        with root.memory.transaction() as db:
            if root.memory._meta(db, "epoch") != scope.epoch:
                raise JournalConflict("ADVISORY_EPOCH_CONFLICT")
            rows = list(
                db.execute(select(tables.meta.c.value).where(tables.meta.c.key.like("nativeAdvisory:%"))).scalars()
            )
        results = []
        for row in rows:
            request = AdvisoryRequest.model_validate_json(row)
            if request.scope.epoch == scope.epoch:
                receipt = root.store.receipt(request.scope, "advisory-result")
                if receipt is None:
                    raise JournalConflict("ADVISORY_COMPLETION_UNCONFIRMED")
                results.append(AdvisoryResult.model_validate(receipt))
        return results
