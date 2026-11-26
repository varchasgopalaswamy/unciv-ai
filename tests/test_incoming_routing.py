"""Incoming routing keeps private audiences, immutable evidence and old deadlines."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from civ_agent import memory_schema as tables
from civ_agent.decisions_policy import DecisionsSettings
from civ_agent.diplomacy_decisions import POLICY_VERSION, parse_answer
from civ_agent.diplomacy_store import event_states
from civ_agent.orchestration.communication_store import CommunicationStore, communication_scope
from civ_agent.orchestration.contracts import CommunicationRun, ModelSelection
from civ_agent.orchestration.incoming_routing import IncomingBatch, NativeIncoming
from civ_agent.orchestration.store import JournalConflict, WorkflowStore
from tests.domain_fixtures import managed
from tests.test_communication_store import event
from tests.test_gateway_registry import registry


def decision(needed="needed", timing="can_wait"):
    return {
        **parse_answer(
            "incoming",
            {
                "answers": [
                    {"name": "response_needed", "type": "choice", "choice": needed, "confidence": 0.99},
                    {"name": "response_timing", "type": "choice", "choice": timing, "confidence": 0.99},
                ]
            },
        ),
        "policyVersion": POLICY_VERSION,
    }


async def setup(domain, tmp_path):
    owner = await registry(domain)
    artifacts = WorkflowStore(tmp_path / "artifacts", player="p0")
    artifacts.bind_epoch("game", "g1")
    scope = communication_scope("game", "p0", "g1")
    store = CommunicationStore(domain.memory, artifacts, scope)
    domain.memory.routing_enabled = True
    social = {
        "civilization": "Rome",
        "chatRecipients": ["Greece", "France"],
        "routingMetadataAvailable": True,
        "trades": [],
    }
    routing = SimpleNamespace(
        settings=DecisionsSettings(decisions_enabled=True),
        human_players=set(),
        classify=AsyncMock(return_value=decision()),
        urgent_thread=AsyncMock(),
    )
    domain.gateway.routing = routing

    async def get(key):
        with domain.memory.transaction() as db:
            return domain.memory._meta(db, key)

    async def put(key, value):
        with domain.memory.transaction() as db:
            domain.memory._set(db, key, value)

    def check(scope):
        if (scope.game, scope.player, scope.epoch) != ("game", "p0", "g1"):
            raise JournalConflict("COMMUNICATION_SCOPE_CONFLICT")

    root = SimpleNamespace(
        civilization="Rome",
        memory=domain.memory,
        io=domain.gateway.io,
        gateway=domain.gateway,
        store=artifacts,
        get=get,
        put=put,
        check_owner=check,
    )
    communication = SimpleNamespace(
        root=root,
        check=check,
        store=lambda value: store,
        connection=AsyncMock(return_value=SimpleNamespace(tool=AsyncMock(side_effect=lambda *_: dict(social)))),
    )
    clock = [110.0]
    native = NativeIncoming(communication, clock=lambda: clock[0])

    def ingest(values, received=100.0):
        store.ingest(values, max(item["id"] for item in values))
        with domain.memory.transaction() as db:
            states = event_states(domain.memory, db)
            for item in values:
                identifier = db.scalar(
                    select(tables.events.c.id).where(tables.events.c.source == "g1:" + str(item["id"]))
                )
                states[str(identifier)]["receivedAt"] = received
            domain.memory._set(db, "routing:events", states)

    run = CommunicationRun(scope=scope, selection=ModelSelection(model="test"))
    return owner, artifacts, store, native, run, routing, social, clock, ingest


def test_batch_classification_is_frozen_and_deadline_cannot_be_renewed_by_a_late_answer(tmp_path):
    async def run():
        async with managed(tmp_path / "domain") as domain:
            owner, artifacts, store, native, request, routing, _, clock, ingest = await setup(domain, tmp_path)
            try:
                ingest([event(77)])  # Native IDs need not equal private database row IDs.
                batch = IncomingBatch(run=request, notices=store.pending())
                first = await native.plan(batch)
                assert not first.ready and first.deadline == 130 and len(first.classifications) == 1
                frozen = artifacts.get(first.classifications[0].scope, first.classifications[0].inputs)
                assert frozen["ids"] == [1] and frozen["notices"][0]["native_id"] == 77
                clock[0] = 115
                assert (await native.plan(batch)).classifications == first.classifications
                await native.classify(first.classifications[0])
                waiting = await native.plan(batch)
                assert not waiting.ready and not waiting.classifications and waiting.deadline == 130
                clock[0] = 131
                assert (await native.plan(batch)).ready == batch.notices
                routing.classify.return_value = decision("not_needed")
                await native.classify(first.classifications[0])
                assert (await native.plan(batch)).ready == batch.notices
                with domain.memory.transaction() as db:
                    assert event_states(domain.memory, db)["1"]["fallback"] == "routing_deadline"
            finally:
                await owner.close()
                artifacts.close()

    asyncio.run(run())


def test_suppression_settles_only_notification_and_keeps_authored_message_for_later_reads(tmp_path):
    async def run():
        async with managed(tmp_path / "domain") as domain:
            owner, artifacts, store, native, request, routing, _, clock, ingest = await setup(domain, tmp_path)
            try:
                ingest([event()])
                batch = IncomingBatch(run=request, notices=store.pending())
                work = (await native.plan(batch)).classifications[0]
                routing.classify.return_value = decision("not_needed")
                await native.classify(work)
                clock[0] = 500
                plan = await native.plan(batch)
                assert plan.consumed == batch.notices and not plan.ready and not plan.classifications
                assert not store.pending()
                assert domain.memory.snapshot("gameplay")["events"][0]["body"]["text"] == "An unverified statement"
            finally:
                await owner.close()
                artifacts.close()

    asyncio.run(run())


@pytest.mark.parametrize("protection", ["human", "trade", "unknown_metadata", "native_action", "non_message"])
def test_protected_events_are_immediate_without_classifier_work(tmp_path, protection):
    async def run():
        async with managed(tmp_path / "domain") as domain:
            owner, artifacts, store, native, request, routing, social, _, ingest = await setup(domain, tmp_path)
            value = event()
            if protection == "human":
                social["humanPlayers"] = ["Greece"]
            elif protection == "trade":
                social["trades"] = [{"status": "PENDING", "participants": ["Rome", "Greece"]}]
            elif protection == "unknown_metadata":
                social.pop("routingMetadataAvailable")
            elif protection == "native_action":
                value["body"]["nativeAction"] = {"kind": "warning"}
            else:
                value["kind"] = "trade"
            try:
                ingest([value])
                batch = IncomingBatch(run=request, notices=store.pending())
                plan = await native.plan(batch)
                assert plan.ready == batch.notices and not plan.classifications
                routing.classify.assert_not_awaited()
            finally:
                await owner.close()
                artifacts.close()

    asyncio.run(run())


def test_coalescing_keeps_old_deadline_and_other_private_audience_out_of_classifier_input(tmp_path):
    async def run():
        async with managed(tmp_path / "domain") as domain:
            owner, artifacts, store, native, request, _, _, clock, ingest = await setup(domain, tmp_path)
            try:
                ingest([event()])
                first = IncomingBatch(run=request, notices=store.pending())
                await native.classify((await native.plan(first)).classifications[0])
                clock[0] = 120
                second, other = event(2), event(3, text="France private secret")
                other["body"]["participants"] = ["Rome", "France"]
                other["body"]["author"] = "France"
                ingest([second, other], received=120)
                plan = await native.plan(IncomingBatch(run=request, notices=store.pending()))
                assert plan.deadline == 130 and len(plan.classifications) == 2
                inputs = [artifacts.get(work.scope, work.inputs) for work in plan.classifications]
                greece = next(value for value in inputs if value["ids"] == [1, 2])
                assert greece["bundle"]["timing"]["remainingBatchSeconds"] == 10
                assert "France private secret" not in str(greece)
            finally:
                await owner.close()
                artifacts.close()

    asyncio.run(run())


@pytest.mark.parametrize("phase", ["plan", "refresh"])
def test_protection_read_outage_requires_immediate_attention(tmp_path, phase):
    async def run():
        async with managed(tmp_path / "domain") as domain:
            owner, artifacts, store, native, request, _, _, _, ingest = await setup(domain, tmp_path)
            try:
                ingest([event()])
                batch = IncomingBatch(run=request, notices=store.pending())
                work = None
                if phase == "refresh":
                    work = (await native.plan(batch)).classifications[0]
                connection = await native.communication.connection("g1")
                connection.tool.side_effect = OSError("temporary host outage")
                if phase == "plan":
                    plan = await native.plan(batch)
                    assert plan.ready == batch.notices and not plan.classifications
                else:
                    assert work is not None
                    await native.classify(work)
                    with domain.memory.transaction() as db:
                        state = event_states(domain.memory, db)["1"]
                    assert state["choice"] == "respond_now" and state["fallback"] == "protection_refresh_failed"
            finally:
                await owner.close()
                artifacts.close()

    asyncio.run(run())
