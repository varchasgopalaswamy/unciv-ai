"""Trusted handoff uses native frozen inputs and host receipts without a local outbox."""

import asyncio
import copy
from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock

import pytest

from civ_agent import memory_schema as tables
from civ_agent.decision_contracts import DecisionContract
from civ_agent.economy_roles import TurnRoles
from civ_agent.gateway_batches import BatchExecutor
from civ_agent.operator_connection import Operator
from civ_agent.orchestration.contracts import GatewayJob, Scope, TurnTicket
from civ_agent.orchestration.coordinator import NativeCoordinator
from civ_agent.orchestration.gateway_state import capture_job
from civ_agent.orchestration.handoff import NativeHandoffs
from civ_agent.orchestration.handoff_contracts import NativeHandoffApplication, NativeHandoffRequest
from civ_agent.orchestration.host_effect_service import NativeCommandService
from civ_agent.orchestration.store import JournalConflict, WorkflowStore
from tests.domain_fixtures import Host


class Ledger(Host):
    def __init__(self):
        super().__init__()
        self.calls, self.effects = [], 0
        self.status = {
            "epoch": "g1",
            "turn": 0,
            "handoff": 0,
            "activeCivilization": "Rome",
            "serverInstance": "s1",
            "leaseId": None,
            "phase": "ACTIVE",
        }
        self.requests = {}

    async def tool(self, name, args):
        payload = args
        self.calls.append((name, copy.deepcopy(payload)))
        if name == "player_receipt":
            previous = self.requests.get(payload["requestId"])
            return copy.deepcopy(previous[1]) if previous and previous[0] == payload else {"error": "NOT_ENACTED"}
        if name == "player_command":
            assert payload["kind"] == "END_TURN" and self.status["handoff"] == 0
            self.effects += 1
            receipt = {
                "requestId": payload["requestId"],
                "epoch": "g1",
                "kind": "END_TURN",
                "turn": 1,
                "revision": "r2",
                "isYourTurn": False,
            }
            self.requests[payload["requestId"]] = (copy.deepcopy(payload), copy.deepcopy(receipt))
            self.status.update(turn=1, handoff=1)
            return receipt
        return await super().tool(name, payload)


async def setup(root_path, ledger, *, existing=False):
    operations = []

    def op(name, **fields):
        operations.append(name)
        if name == "activate":
            assert ledger.status["handoff"] == 0, "Expired activation must never be resumed"
        assert name in {"status", "connect", "activate"}
        return {"status": copy.deepcopy(ledger.status), "playerToken": "active"}

    store = WorkflowStore(root_path / "store", player="p0")
    root = NativeCoordinator(
        game="game",
        player="p0",
        civilization="Rome",
        state_dir=root_path / "coordinator",
        store=store,
        operator=cast(Operator, SimpleNamespace(host_url="https://host.invalid", call=op)),
        host_ca=root_path / "ca",
    )
    root.connection_factory = lambda *_: ledger
    root.memory.bind("g1")
    store.bind_epoch("game", "g1")
    ledger.status["leaseId"] = root.game_state["lease"]
    ticket = TurnTicket(
        scope=Scope(game="game", player="p0", epoch="g1", activation="turn1"), turn=0, handoff=0, index=0
    )
    main = GatewayJob(scope=ticket.scope.model_copy(update={"activation": "main1"}), job="main1", purpose="gameplay")
    if not existing:
        job = await root.gateway.open(main.job, main.purpose, "g1", ledger, {}, 0)
        delivered = BatchExecutor(root.gateway, job, DecisionContract(job.purpose, [])).delivered_access
        await root.registry.register(main, capture_job(job, delivered_access=delivered), [])
        record = {
            "activationId": "turn1",
            "epoch": "g1",
            "gameTurn": 0,
            "ticket": ticket.model_dump(mode="json"),
            "roleJobs": {"gameplay": main.model_dump(mode="json")},
            "requests": {"gameplay:0": main.model_dump(mode="json")},
            "bootstraps": {},
        }
        roles = TurnRoles(root, record)
        roles.jobs["gameplay"] = job
        roles.ready = {key: True for key in roles.ready}
        root.record, root.turn_roles = record, roles
        root.gateway.role_coordinator = roles
        root.connection, root.connection_key = ledger, ("s1", "active")
        await root._save(record)
    service = NativeHandoffs(root, seal_key=NativeCommandService(root.registry).seal_key)
    return root, store, service, ticket, main, operations


def assert_no_handoff_journal(root):
    with root.memory.transaction() as db:
        assert all(
            not db.execute(table.select()).all() for table in (tables.calls, tables.outbox, tables.decision_journal)
        )


def test_frozen_handoff_recovers_after_host_commit_and_restart_without_old_activation_or_receipt_copy(
    tmp_path, monkeypatch
):
    async def run():
        monkeypatch.setattr(TurnRoles, "synchronize_war", AsyncMock())
        monkeypatch.setattr(TurnRoles, "synchronize_economy", AsyncMock())
        ledger = Ledger()
        root, store, service, ticket, main, _ = await setup(tmp_path, ledger)
        try:
            prepared = await service.prepare(NativeHandoffRequest(ticket=ticket, attempt=0))
            effect = prepared.effect
            assert effect is not None and effect.prepared.payload is not None
            assert effect.prepared.payload["reviewId"] == "review1"
            assert_no_handoff_journal(root)

            async def lost_ack(*_):
                raise OSError("Lost host execution acknowledgement")

            root.handoff_checkpoint = lost_ack
            with pytest.raises(OSError):
                await service.execute(effect)
            assert ledger.effects == 1
        finally:
            await root.close()
            store.close()
        root, store, service, _, _, operations = await setup(tmp_path, ledger, existing=True)
        try:
            receipt = await service.execute(effect)
            assert ledger.effects == 1 and "activate" not in operations
            application = NativeHandoffApplication(effect=effect, receipt=receipt)
            result = await service.apply(application)
            assert result.committed and (result.turn, result.handoff) == (1, 1)
            assert await service.apply(application) == result
            assert (await service.prepare(effect.request)).result == result
            assert root.registry.load(main).retired
            assert await service.finish(effect) == await service.finish(effect) == result
            assert root.registry.load(main).retired and "activate" not in operations
            saved = await root.get("nativeTurn:turn1")
            assert saved["lifecycleFinalized"] and saved["nativeHandoff"]["operationId"] == receipt["requestId"]
            assert_no_handoff_journal(root)
        finally:
            await root.close()
            store.close()

    asyncio.run(run())


@pytest.mark.parametrize("damage", ["seal", "receipt", "uncertain", "request_conflict", "epoch"])
def test_handoff_integrity_and_uncertainty_never_authorize_another_effect(tmp_path, monkeypatch, damage):
    async def run():
        monkeypatch.setattr(TurnRoles, "synchronize_war", AsyncMock())
        monkeypatch.setattr(TurnRoles, "synchronize_economy", AsyncMock())
        ledger = Ledger()
        root, store, service, ticket, _, _ = await setup(tmp_path, ledger)
        try:
            effect = (await service.prepare(NativeHandoffRequest(ticket=ticket, attempt=0))).effect
            assert effect is not None
            if damage == "seal":
                changed = effect.model_copy(update={"seal": "f" * 64})
                with pytest.raises(JournalConflict):
                    await service.execute(changed)
            else:
                receipt = await service.execute(effect)
                if damage == "receipt":
                    receipt = {**receipt, "requestId": "foreign"}
                if damage == "uncertain":
                    receipt = {"error": "NOT_ENACTED"}
                if damage == "request_conflict":
                    receipt = {"error": "REQUEST_ID_CONFLICT"}
                if damage == "epoch":
                    ledger.status["epoch"] = "new"
                with pytest.raises(JournalConflict):
                    await service.apply(NativeHandoffApplication(effect=effect, receipt=receipt))
            assert ledger.effects <= 1 and not (await root.get("nativeTurn:turn1")).get("committedHandoff")
            assert_no_handoff_journal(root)
        finally:
            await root.close()
            store.close()

    asyncio.run(run())


@pytest.mark.parametrize("defer", [False, True])
def test_native_review_reopens_required_choices_or_preserves_explicit_deferral(tmp_path, monkeypatch, defer):
    async def run():
        monkeypatch.setattr(TurnRoles, "synchronize_war", AsyncMock())
        monkeypatch.setattr(TurnRoles, "synchronize_economy", AsyncMock())
        ledger = Ledger()
        ledger.view.update(canEndTurn=False, pending=[{"type": "CONSTRUCTION"}], turnControls={"canDeferChoices": True})
        root, store, service, ticket, _, _ = await setup(tmp_path, ledger)
        try:
            roles = root.turn_roles
            assert roles is not None
            if defer:
                roles.deferral = {"deferChoices": True, "deferReason": "Keep native queues unchanged"}
            prepared = await service.prepare(NativeHandoffRequest(ticket=ticket, attempt=0))
            assert (prepared.effect is not None) is defer
            if defer:
                assert prepared.effect is not None and prepared.effect.prepared.payload is not None
                assert prepared.effect.prepared.payload["deferChoices"] is True
            else:
                assert prepared.result is not None
                assert not prepared.result.committed and not roles.ready["economy"]
            assert ledger.effects == 0
            assert_no_handoff_journal(root)
        finally:
            await root.close()
            store.close()

    asyncio.run(run())


def test_uncertain_lookup_never_sends_and_cleanup_ack_retry_never_reactivates(tmp_path, monkeypatch):
    async def run():
        monkeypatch.setattr(TurnRoles, "synchronize_war", AsyncMock())
        monkeypatch.setattr(TurnRoles, "synchronize_economy", AsyncMock())
        ledger = Ledger()
        root, store, service, ticket, _, operations = await setup(tmp_path, ledger)
        try:
            effect = (await service.prepare(NativeHandoffRequest(ticket=ticket, attempt=0))).effect
            assert effect is not None
            original = ledger.tool

            async def unknown(name, args):
                return {"error": "INTERNAL_ERROR"} if name == "player_receipt" else await original(name, args)

            ledger.tool = unknown
            with pytest.raises(JournalConflict, match="UNCERTAIN"):
                await service.execute(effect)
            assert ledger.effects == 0
            ledger.tool = original
            receipt = await service.execute(effect)
            await service.apply(NativeHandoffApplication(effect=effect, receipt=receipt))
            operations.clear()
            retired = root.registry._retire
            root.registry._retire = lambda identity: (_ for _ in ()).throw(OSError("Interrupted retirement"))
            with pytest.raises(OSError):
                await service.finish(effect)
            root.registry._retire = retired
            assert (await service.finish(effect)).committed
            assert "activate" not in operations and ledger.effects == 1
            assert_no_handoff_journal(root)
        finally:
            await root.close()
            store.close()

    asyncio.run(run())
