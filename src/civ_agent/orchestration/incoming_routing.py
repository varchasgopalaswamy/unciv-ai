"""Permitted incoming evidence and policy for the local controller."""

import time
from typing import Annotated, Literal

import aiohttp
from pydantic import Field
from sqlalchemy import select, update

from civ_agent import memory_schema as tables
from civ_agent.conversation_context import audience
from civ_agent.diplomacy_decisions import BATCH_SECONDS, fallback
from civ_agent.diplomacy_store import DiplomacyStore, event_states
from civ_agent.orchestration.communication_store import notifications
from civ_agent.orchestration.contracts import (
    CommunicationRun,
    Digest,
    EventNotice,
    NativeAuxiliaryTask,
    Reference,
    Scope,
    digest,
)
from civ_agent.orchestration.store import JournalConflict
from civ_agent.routing_policy import protected_thread, tuple_thread, validate_decision
from civ_agent.schemas import BoundaryModel, Identifier


class IncomingWork(BoundaryModel):
    version: Literal[1] = 1
    scope: Scope
    operation_id: Identifier
    thread: Digest
    inputs: Reference
    task: NativeAuxiliaryTask | None = Field(default=None, exclude_if=lambda value: value is None)


class IncomingBatch(BoundaryModel):
    version: Literal[1] = 1
    run: CommunicationRun
    notices: Annotated[list[EventNotice], Field(min_length=1)]


class IncomingPlan(BoundaryModel):
    version: Literal[1] = 1
    ready: list[EventNotice] = Field(default_factory=list)
    consumed: list[EventNotice] = Field(default_factory=list)
    classifications: list[IncomingWork] = Field(default_factory=list)
    deadline: Annotated[float, Field(ge=0, allow_inf_nan=False)] | None = None


class NativeIncoming:
    def __init__(self, communication, *, clock=time.time):
        self.communication, self.root, self.clock = communication, communication.root, clock

    def _rows(self, batch):
        store = self.communication.store(batch.run.scope)
        with self.root.memory.transaction() as db:
            states = event_states(self.root.memory, db)
            rows = []
            for notice in batch.notices:
                notification = store.row(db, notice)
                native = (
                    db.execute(select(tables.events).where(tables.events.c.source == notification["source"]))
                    .mappings()
                    .one()
                )
                event = self.root.store.get(notice.scope, notice.content)
                rows.append((notice, native["id"], event, states.get(str(native["id"]), {}), notification))
            return rows

    def _consume(self, batch, notices):
        store = self.communication.store(batch.run.scope)
        with self.root.memory.transaction() as db:
            states = event_states(self.root.memory, db)
            for notice in notices:
                row = store.row(db, notice)
                native_id = db.scalar(select(tables.events.c.id).where(tables.events.c.source == row["source"]))
                state = states.get(str(native_id), {})
                if not row["handled"] and not (
                    state.get("choice") == "no_separate_response" or state.get("completed") or state.get("attempted")
                ):
                    raise JournalConflict("INCOMING_ROUTING_NOT_SETTLED")
                db.execute(update(notifications).where(notifications.c.id == notice.id).values(handled=1, signaled=1))

    async def _social(self, scope):
        connection = await self.communication.connection(scope.epoch)
        social = await connection.tool("player_communication", {"op": "state", "epoch": scope.epoch})
        if social.get("civilization") != self.root.civilization:
            raise JournalConflict("PLAYER_CREDENTIAL_MISMATCH")
        return social

    async def plan(self, batch: IncomingBatch) -> IncomingPlan:
        self.communication.check(batch.run.scope)
        routing = self.root.gateway.routing
        rows = await self.root.io.call(self._rows, batch)
        plan = IncomingPlan()
        if routing is None:
            plan.ready = [row[0] for row in rows if not row[4]["handled"]]
            plan.consumed = [row[0] for row in rows if row[4]["handled"]]
            return plan
        try:
            social = await self._social(batch.run.scope)
        except (OSError, ValueError, TimeoutError, aiohttp.ClientError):
            social = {}  # Missing protection metadata requires immediate attention.
        now = self.clock()
        domain = DiplomacyStore(self.root.memory, self.clock)
        groups = {}
        for notice, identifier, event, state, notification in rows:
            if notification["handled"] or state.get("completed") or state.get("attempted"):
                plan.consumed.append(notice)
                continue
            thread = audience(event["body"], self.root.civilization)
            protected = (
                event["kind"] != "message"
                or bool(event["body"].get("nativeAction"))
                or thread is None
                or protected_thread(thread, social, player=self.root.civilization, human_players=routing.human_players)
            )
            deadline = (
                min(state.get("deadline", float("inf")), state["receivedAt"] + BATCH_SECONDS)
                if "receivedAt" in state
                else now
            )
            if state.get("choice") == "no_separate_response" and not protected:
                plan.consumed.append(notice)
                continue
            if protected or deadline <= now:
                reason = "protected" if protected else "routing_deadline"
                if (
                    state.get("choice") != "respond_now"
                    or reason == "routing_deadline"
                    and state.get("fallback") != reason
                ):
                    await self.root.io.call(
                        domain.route,
                        batch.run.scope.epoch,
                        [identifier],
                        fallback("incoming", reason),
                        batch.run.scope.identity(reason, notice.id),
                    )
                plan.ready.append(notice)
                if thread is not None:
                    await routing.urgent_thread(batch.run.scope.epoch, thread)
                continue
            if state.get("choice") == "respond_now":
                plan.ready.append(notice)
                continue
            plan.deadline = min(plan.deadline or deadline, deadline)
            groups.setdefault(thread, []).append((notice, identifier, event, state, notification))
        for thread, pending in groups.items():
            if all(row[3].get("choice") for row in pending):
                continue
            notices = [row[0] for row in pending]
            scope = batch.run.scope.model_copy(update={"activation": digest([n.id for n in notices])})
            operation = scope.identity("incoming-routing", "classification")
            saved = await self.root.get("nativeIncomingRouting:" + operation)
            if saved is None:
                bundle = await self.root.io.call(
                    domain.bundle, thread, [row[2]["body"] for row in pending], epoch=batch.run.scope.epoch
                )
                value = {
                    "communication_scope": batch.run.scope.model_dump(mode="json"),
                    "notices": [notice.model_dump(mode="json") for notice in notices],
                    "ids": [row[1] for row in pending],
                    "thread": thread,
                    "bundle": bundle,
                }
                work = IncomingWork(
                    scope=scope,
                    operation_id=operation,
                    thread=digest(thread),
                    inputs=self.root.store.put(scope, value),
                    task=NativeAuxiliaryTask(
                        kind="incoming_routing",
                        key=digest([[row[4]["source"], row[0].content.sha256] for row in pending]),
                    ),
                )
                await self.root.put("nativeIncomingRouting:" + operation, work.model_dump(mode="json"))
            else:
                work = IncomingWork.model_validate(saved)
                if (work.scope, work.operation_id, work.thread) != (scope, operation, digest(thread)):
                    raise JournalConflict("INCOMING_ROUTING_BINDING_CONFLICT")
            plan.classifications.append(work)
        await self.root.io.call(self._consume, batch, plan.consumed)
        return plan

    async def classify(self, work: IncomingWork):
        self.root.check_owner(work.scope)
        saved = await self.root.get("nativeIncomingRouting:" + work.operation_id)
        if saved != work.model_dump(mode="json"):
            raise JournalConflict("INCOMING_ROUTING_BINDING_CONFLICT")
        value = self.root.store.get(work.scope, work.inputs)
        scope = Scope.model_validate(value["communication_scope"])
        self.communication.check(scope)
        if (scope.game, scope.player, scope.epoch) != (work.scope.game, work.scope.player, work.scope.epoch):
            raise JournalConflict("INCOMING_ROUTING_BINDING_CONFLICT")
        routing = self.root.gateway.routing
        if routing is None:
            raise JournalConflict("INCOMING_ROUTING_UNAVAILABLE")
        decision = await routing.classify(work.scope, work.operation_id, "incoming", value["bundle"], task=work.task)
        decision = validate_decision("incoming", decision, routing.settings)
        thread = tuple_thread(value["thread"])
        try:
            social = await self._social(scope)
            if protected_thread(thread, social, player=self.root.civilization, human_players=routing.human_players):
                decision = fallback("incoming", "protected")
        except (OSError, ValueError, TimeoutError, aiohttp.ClientError):
            decision = fallback("incoming", "protection_refresh_failed")
        await self.root.io.call(
            DiplomacyStore(self.root.memory, self.clock).route,
            scope.epoch,
            value["ids"],
            decision,
            work.operation_id,
        )
        if decision["choice"] == "respond_now":
            await routing.urgent_thread(scope.epoch, thread)
