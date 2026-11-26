"""Identical restored tactical facts require a new epoch's Peace authorization."""

import asyncio
import copy
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from civ_agent import memory_schema as tables
from civ_agent.civilization_state import CivilizationState
from civ_agent.orchestration.contracts import GatewayCall, GatewayJob, ModelSelection, Scope
from civ_agent.orchestration.gateway_execution import native_call_id
from civ_agent.orchestration.peace_authorization import NativePeace
from civ_agent.orchestration.peace_wait_contracts import PeaceApprovalWait, approved_peace_wait
from civ_agent.orchestration.store import JournalConflict, WorkflowStore
from civ_agent.peace_contracts import peace_terms
from civ_agent.service_io import ServiceIO
from civ_agent.war_state import WarStore
from tests.test_military_roles import position
from tests.test_peace_preflight import sealed


@asynccontextmanager
async def peace_domain(tmp_path):
    memory = CivilizationState(tmp_path / "domain", "Rome")
    memory.bind("e1")
    store = WorkflowStore(tmp_path / "workflow", player="p0")
    store.bind_epoch("game", "e1")
    io = ServiceIO()
    view = {**position(), "epoch": "e1"}

    async def get(key):
        with memory.transaction() as db:
            return copy.deepcopy(memory._meta(db, key))

    async def put(key, value):
        with memory.transaction() as db:
            memory._set(db, key, value)

    def check_owner(scope):
        assert (scope.game, scope.player) == ("game", "p0")

    root = SimpleNamespace(
        game="game",
        player="p0",
        external_lease_id="shared",
        memory=memory,
        store=store,
        io=io,
        get=get,
        put=put,
        check_owner=check_owner,
        gateway=SimpleNamespace(wars=WarStore(memory), consultation_hold=asyncio.Event()),
        get_selection=lambda _: ModelSelection(model="frozen"),
        get_provider_contract=lambda _: None,
        get_request_limit=lambda _: 2,
    )
    service = NativePeace(root)
    connection = SimpleNamespace(tool=AsyncMock(side_effect=lambda *_: copy.deepcopy(view)))
    payload = {"partner": "Greece", "give": [{"type": "PeaceProposal", "amount": 1}], "receive": []}
    terms = peace_terms("trade_offer", payload)
    assert terms is not None

    def job(epoch):
        return SimpleNamespace(epoch=epoch, connection=connection, wait_started=None, completed_wait=0)

    try:
        yield SimpleNamespace(service=service, root=root, view=view, payload=payload, terms=terms, job=job)
    finally:
        await io.close()
        store.close()
        memory.engine.dispose()


async def frozen(domain, epoch, *, managed=True):
    prepared = await domain.service._prepare(domain.job(epoch), domain.terms, managed=managed)
    assert prepared is not None
    return prepared[-1]


def test_long_sdk_child_approval_matches_bounded_native_identity(tmp_path):
    async def run():
        async with peace_domain(tmp_path) as domain:
            consultation = await frozen(domain, "e1")
            identity = GatewayJob(scope=consultation.scope, job="approved-job", purpose="gameplay")
            child_id = "parent" * 40 + "__1"
            call = GatewayCall(
                call_id=native_call_id(child_id),
                model_step=1,
                decision={
                    "kind": "act",
                    "operations": [{"tool": "trade_offer", "arguments": domain.payload}],
                },
            )
            wait = PeaceApprovalWait(
                identity=identity, tool="act", call=call, run_id="sdk-run", requests=[consultation]
            )
            assert (
                approved_peace_wait(
                    identity,
                    child_id,
                    "act",
                    call.decision,
                    approved=True,
                    metadata={"nativePeace": wait.model_dump(mode="json")},
                )
                == wait
            )
            with pytest.raises(JournalConflict, match="APPROVAL_CALL_CONFLICT"):
                approved_peace_wait(
                    identity,
                    child_id + "other",
                    "act",
                    call.decision,
                    approved=True,
                    metadata={"nativePeace": wait.model_dump(mode="json")},
                )

    asyncio.run(run())


def test_identical_restored_facts_require_new_epoch_consultation_and_reject_old_approval(tmp_path):
    async def run():
        async with peace_domain(tmp_path) as domain:
            service, root = domain.service, domain.root
            first = await frozen(domain, "e1")
            first_result = sealed(root.store, first)
            assert await frozen(domain, "e1") == first
            assert await service.authorize(domain.job("e1"), "trade_offer", copy.deepcopy(domain.payload)) is None
            first_key = service._key("e1", first.basis)
            original = await root.get(first_key)
            # Retain a basis-only staged alias in the actual domain checkpoint.
            await root.put("nativePeace:" + first.basis, {**original, "result": first_result.model_dump(mode="json")})
            assert await service.results(first.scope) == [first_result]
            checkpoint = tmp_path / "before-rewind.json"
            checksum = root.memory.backup(checkpoint)
            root.memory.restore(checkpoint, checksum, "e2")
            root.memory.pause(False)
            root.store.bind_epoch("game", "e2", previous="e1")
            domain.view["epoch"] = "e2"

            second = await frozen(domain, "e2")
            assert second.basis == first.basis
            assert second.scope.game == first.scope.game == "game"
            assert (first.scope.epoch, second.scope.epoch) == ("e1", "e2")
            assert second.scope != first.scope and second.inputs != first.inputs
            assert service._key("e2", second.basis) != first_key
            assert await root.get(first_key) == original
            assert await frozen(domain, "e2") == second
            assert await service.results(second.scope) == []
            with pytest.raises(JournalConflict, match="PEACE_RUNNER_NOT_CONFIGURED"):
                await service.authorize(domain.job("e2"), "trade_offer", copy.deepcopy(domain.payload))

            identity = GatewayJob(
                scope=Scope(game="game", player="p0", epoch="e1", activation="original-role"),
                job="original-job",
                purpose="gameplay",
            )
            call = GatewayCall(
                call_id="original-call",
                model_step=1,
                decision={"kind": "act", "operations": [{"tool": "trade_offer", "arguments": domain.payload}]},
            )
            old = PeaceApprovalWait(identity=identity, tool="act", call=call, run_id="sdk-run", requests=[first])
            new_identity = identity.model_copy(update={"scope": identity.scope.model_copy(update={"epoch": "e2"})})
            with pytest.raises(JournalConflict, match="PEACE_APPROVAL_CALL_CONFLICT"):
                approved_peace_wait(
                    new_identity,
                    call.call_id,
                    "act",
                    call.decision,
                    approved=True,
                    metadata={"nativePeace": old.model_dump(mode="json")},
                )
            # Only the new scope's sealed receipt can authorize the new timeline.
            second_result = sealed(root.store, second)
            assert await service.authorize(domain.job("e2"), "trade_offer", copy.deepcopy(domain.payload)) is None
            assert await service.results(second.scope) == [second_result]
            assert await frozen(domain, "e2") == second
            assert service.consult is None

    asyncio.run(run())


@pytest.mark.parametrize("damage", ["game", "player", "epoch", "basis", "activation"])
def test_canonical_peace_record_binding_tamper_fails_closed(tmp_path, damage):
    async def run():
        async with peace_domain(tmp_path) as domain:
            request = await frozen(domain, "e1")
            key = domain.service._key("e1", request.basis)
            saved = await domain.root.get(key)
            if damage == "basis":
                saved["request"]["basis"] = "0" * 64
            else:
                saved["request"]["scope"][damage] = "other"
            await domain.root.put(key, saved)
            with pytest.raises(JournalConflict, match="PEACE_REQUEST_BINDING_CONFLICT"):
                await frozen(domain, "e1")
            assert domain.service.consult is None

    asyncio.run(run())


@pytest.mark.parametrize("damage", [None, "game", "player", "epoch", "basis", "activation"])
@pytest.mark.parametrize("managed", [False, True])
def test_basis_only_staged_record_is_reused_only_for_exact_same_epoch_owner_scope(tmp_path, damage, managed):
    async def run():
        async with peace_domain(tmp_path) as domain:
            first = await frozen(domain, "e1", managed=managed)
            key = domain.service._key("e1", first.basis)
            saved = await domain.root.get(key)
            if not managed:
                # A staged finite-pilot record can predate managed/proof fields.
                for field in ("managed", "provider_contract", "military_provider_contract"):
                    saved["request"].pop(field)
            with domain.root.memory.transaction() as db:
                db.execute(tables.meta.delete().where(tables.meta.c.key == key))
            if damage == "basis":
                saved["request"]["basis"] = "0" * 64
            elif damage is not None:
                saved["request"]["scope"][damage] = "other"
            await domain.root.put("nativePeace:" + first.basis, saved)
            domain.root.get_selection = lambda _: ModelSelection(model="fresh")
            resumed = await frozen(domain, "e1", managed=managed)
            if damage is None:
                assert resumed == first and await domain.root.get(key) is None
            else:
                assert resumed.scope == first.scope and resumed.basis == first.basis
                assert resumed.selection.model == "fresh" and await domain.root.get(key) is not None
            assert await frozen(domain, "e1", managed=managed) == resumed
            assert domain.service.consult is None

    asyncio.run(run())
