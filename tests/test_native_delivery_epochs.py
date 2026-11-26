"""Restored authored drafts retain history without arming target-epoch sends."""

import asyncio
import copy
import hashlib
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import func, select

from civ_agent import memory_schema as tables
from civ_agent.civilization_state import CivilizationState
from civ_agent.decisions_policy import DecisionsSettings
from civ_agent.diplomacy_decisions import BATCH_SECONDS
from civ_agent.memory_store import RecordStore
from civ_agent.orchestration.contracts import Scope, digest
from civ_agent.orchestration.delivery_contracts import OutgoingDelivery, OutgoingDraft
from civ_agent.orchestration.routing import NativeRouting
from civ_agent.orchestration.store import JournalConflict, WorkflowStore
from civ_agent.service_io import ServiceIO

THREAD = (False, ("Greece", "Rome"))


@asynccontextmanager
async def native(tmp_path):
    memory = CivilizationState(tmp_path / "domain", "Rome")
    memory.bind("e2")
    with memory.transaction() as db:
        memory._set(db, "nativeGatewayOwner", {"version": 1, "game": "game", "player": "p0"})
    artifacts = WorkflowStore(tmp_path / "artifacts", player="p0")
    artifacts.bind_epoch("game", "e2")
    io = ServiceIO()

    async def get(key, default=None):
        with memory.transaction() as db:
            return memory._meta(db, key, default)

    async def put(key, value):
        with memory.transaction() as db:
            memory._set(db, key, value)

    def owner(scope):
        if (scope.game, scope.player) != ("game", "p0"):
            raise JournalConflict("COORDINATOR_OWNER_CONFLICT")

    gateway = SimpleNamespace(
        dispatch=asyncio.Lock(), paused=False, checkpoint_requested=False, run_failure=None, check=lambda _: None
    )
    host = SimpleNamespace(tool=AsyncMock(side_effect=AssertionError("No host operation expected")))
    root = SimpleNamespace(
        game="game",
        player="p0",
        civilization="Rome",
        memory=memory,
        io=io,
        store=artifacts,
        gateway=gateway,
        get=get,
        put=put,
        check_owner=owner,
        communication=None,
        connection=host,
    )
    broker = SimpleNamespace(route=AsyncMock(), lookup_route=AsyncMock())
    router = NativeRouting(root, broker, DecisionsSettings(decisions_enabled=True))
    router.store.clock = lambda: 1000.0
    scope = Scope(game="game", player="p0", epoch="e2", activation="communication")
    await router.set_hold(scope, False)
    try:
        yield SimpleNamespace(root=root, router=router, host=host, broker=broker, scope=scope)
    finally:
        await router.close()
        await io.close()
        artifacts.close()
        memory.engine.dispose()


def historical(r, *, epoch="e1", status="queued", **extra):
    identifier = "delivery-" + digest([epoch, status, extra])[:24]
    request = OutgoingDraft(
        scope=Scope(game="game", player="p0", epoch=epoch, activation=digest(["outgoing", identifier])),
        delivery_id=identifier,
    )
    draft = {
        "deliveryId": identifier,
        "payload": {"epoch": epoch, "op": "send", "text": "Perhaps peace, if circumstances permit.", "to": "Greece"},
        "thread": THREAD,
        "createdAt": 1.0,
        "deadline": 2.0,
        "status": status,
        "owner": "Rome",
        "sourceJob": "historical-source-job",
        **extra,
    }
    with r.root.memory.transaction() as db:
        r.root.memory._set(db, "nativeOutgoing:" + identifier, request.model_dump(mode="json"))
        RecordStore(db).write(identifier, "delivery", draft)
    return request, draft


@pytest.mark.parametrize(
    "status,extra",
    [
        ("classifying", {}),
        ("queued", {}),
        ("queued", {"urgent": True}),
        ("queued", {"override": "immediate"}),
        ("omitted", {}),
        ("omitted", {"override": "immediate"}),
        ("sending", {"requestId": "old-native-id"}),
        ("sent", {"message": {"message": "original-message", "text": "Exact original text"}}),
        ("failed", {"error": "ORIGINAL_NATIVE_ERROR"}),
        ("cancelled", {}),
    ],
)
def test_old_epoch_drafts_are_unarmed_in_all_active_readers_and_remain_exact_history(tmp_path, status, extra):
    async def run():
        async with native(tmp_path) as r:
            request, _ = historical(r, status=status, **extra)
            before = copy.deepcopy(r.router.store.drafts())
            await r.router.recover(r.scope)
            await r.router.urgent_thread("e2", THREAD)
            await r.router.drain(r.scope)
            await r.router.set_hold(r.scope, True)
            await r.router.settle_sending(r.scope)
            assert await r.router.statuses() == []
            assert r.router.store.bundle(THREAD, [], epoch="e2")["queuedDrafts"] == []
            for action in ("status", "cancel", "immediate"):
                assert await r.router.control(
                    SimpleNamespace(epoch="e2"), {"deliveryId": request.delivery_id, "action": action}
                ) == {"error": "UNKNOWN_DELIVERY", "applied": False}
            assert r.router.store.drafts() == before and r.router.store.get(request.delivery_id) == before[0]
            assert not r.router.tasks
            r.host.tool.assert_not_awaited()
            r.broker.route.assert_not_awaited()
            with r.root.memory.transaction() as db:
                assert db.scalar(select(func.count()).select_from(tables.jobs)) == 0
                assert db.scalar(select(func.count()).select_from(tables.outbox)) == 0
            with pytest.raises(JournalConflict, match="ROUTING_EPOCH_CONFLICT"):
                await r.router.prepare_delivery(OutgoingDelivery(request=request, deadline=2, force=True))

    asyncio.run(run())


def test_new_draft_has_fresh_deadline_classifier_input_and_order_despite_expired_old_queue(tmp_path):
    async def run():
        async with native(tmp_path) as r:
            historical(r, urgent=True, override="immediate")
            r.root.memory.start_job("new-source-job", "communication", {})
            job = SimpleNamespace(id="new-source-job", epoch="e2")
            identifier = "delivery-" + hashlib.sha256(b"new-source-job:new-call").hexdigest()[:24]
            request = OutgoingDraft(
                scope=r.scope.model_copy(update={"activation": digest(["outgoing", identifier])}),
                delivery_id=identifier,
            )
            await r.root.put("nativeOutgoing:" + identifier, request.model_dump(mode="json"))
            payload = {"epoch": "e2", "op": "send", "text": "Freshly authored decision", "to": "Greece"}
            draft = r.router.store.draft(job, "new-call", payload, THREAD, epoch="e2")
            assert draft["createdAt"] == 1000 and draft["deadline"] == 1000 + BATCH_SECONDS
            r.router.protected_now = AsyncMock(return_value=False)
            plan = await r.router.prepare(request)
            assert plan.routing is not None
            submission = r.root.store.get(plan.routing.scope, plan.routing.submission)
            assert submission["bundle"]["queuedDrafts"] == []
            assert submission["bundle"]["proposedMessage"]["text"] == payload["text"]
            assert submission["bundle"]["timing"]["remainingBatchSeconds"] == BATCH_SECONDS
            assert (
                await r.router.delivery_blocked(
                    OutgoingDelivery(request=request, deadline=draft["deadline"], force=True, ordered=True), draft
                )
                is None
            )
            r.host.tool.assert_not_awaited()
            r.broker.route.assert_not_awaited()

    asyncio.run(run())


@pytest.mark.parametrize("damage", ["game", "player", "owner", "missing_payload_epoch", "missing_binding", "mismatch"])
def test_current_or_unknown_ownership_fails_closed_instead_of_hiding_corrupt_draft(tmp_path, damage):
    async def run():
        async with native(tmp_path) as r:
            request, draft = historical(r, epoch="e2")
            if damage in {"game", "player"}:
                request.scope = request.scope.model_copy(update={damage: "foreign"})
            elif damage == "owner":
                draft["owner"] = "Greece"
            elif damage == "missing_payload_epoch":
                draft["payload"].pop("epoch")
            elif damage == "mismatch":
                draft["payload"]["epoch"] = "e1"
            with r.root.memory.transaction() as db:
                r.root.memory._set(
                    db,
                    "nativeOutgoing:" + request.delivery_id,
                    None if damage == "missing_binding" else request.model_dump(mode="json"),
                )
                RecordStore(db).write(request.delivery_id, "delivery", draft)
            for operation in (
                r.router.statuses,
                lambda: r.router.recover(r.scope),
                lambda: r.router.urgent_thread("e2", THREAD),
                lambda: r.router.control(SimpleNamespace(epoch="e2"), {"deliveryId": request.delivery_id}),
            ):
                with pytest.raises(JournalConflict):
                    await operation()
            with pytest.raises(JournalConflict):
                r.router.store.bundle(THREAD, [], epoch="e2")
            assert not r.router.tasks
            r.host.tool.assert_not_awaited()

    asyncio.run(run())
