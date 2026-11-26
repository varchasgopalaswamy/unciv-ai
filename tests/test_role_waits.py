import asyncio
from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock

import pytest

from civ_agent import memory_schema as tables
from civ_agent.decision_contracts import DecisionContract
from civ_agent.economy_roles import TurnRoles
from civ_agent.gateway_batches import BatchExecutor
from civ_agent.operator_connection import Operator
from civ_agent.orchestration.contracts import GatewayJob, ModelSelection, PrepareRole, RoleWait, Scope, TurnTicket
from civ_agent.orchestration.coordinator import NativeCoordinator
from civ_agent.orchestration.gateway_execution import operation_identity
from civ_agent.orchestration.gateway_state import capture_job
from civ_agent.orchestration.handoff_contracts import NativeHandoffApplication, NativeHandoffRequest
from civ_agent.orchestration.store import WorkflowStore
from tests.domain_fixtures import StartupHost
from tests.step_history import save_wait
from tests.test_native_handoff import Ledger, setup


async def runtime(tmp_path, *, recover=False):
    store = WorkflowStore(tmp_path / "worker", player="p0")
    root = NativeCoordinator(
        game="game",
        player="p0",
        civilization="Rome",
        state_dir=tmp_path / "coordinator",
        store=store,
        operator=cast(Operator, SimpleNamespace()),
        host_ca=tmp_path / "ca.pem",
    )
    host = StartupHost(
        SimpleNamespace(service=SimpleNamespace(records={"turn0": {"epoch": "g1", "gameTurn": 0}}, active_id="turn0"))
    )
    root.activate = AsyncMock(return_value=host)
    scope = Scope(game="game", player="p0", epoch="g1", activation="main-role")
    binding = GatewayJob(scope=scope, job="main-job", purpose="gameplay")
    if not recover:
        root.memory.bind("g1")
        store.bind_epoch("game", "g1")
        ticket = TurnTicket(scope=scope.model_copy(update={"activation": "turn0"}), turn=0, handoff=0, index=0)
        record = {
            "epoch": "g1",
            "activationId": "turn0",
            "gameTurn": 0,
            "settings": {},
            "ticket": ticket.model_dump(mode="json"),
            "roleJobs": {"gameplay": binding.model_dump(mode="json")},
        }
        root.record = record
        root.turn_roles = TurnRoles(root, record)
        root.gateway.role_coordinator = root.turn_roles
        main = await root.gateway.open(binding.job, "gameplay", "g1", host, {}, 0)
        root.turn_roles.register(main)
        await root.turn_roles.initialize_economy(await host.tool("player_turn", {}))
        root.turn_roles.requests = [{"id": "r1", "status": "pending", "priority": "routine"}]
        root.turn_roles.ready["economy"] = False
        await root.gateway.call(main, "player_turn", {}, "startup:turn")
        catalogue = await root.gateway.tools(main)
        contract = DecisionContract(main.purpose, catalogue)
        state = capture_job(main, delivered_access=BatchExecutor(root.gateway, main, contract).delivered_access)
        await root.registry.register(binding, state, catalogue)
        await root.put("nativeRoleTurn:" + main.id, "turn0")
        await root._save(record)
    return root, store, binding


async def register_wait(root, binding):
    result = await root.registry(binding, "wait1", 1, {"kind": "wait", "arguments": {"requestIds": ["r1"]}})
    assert result.result["receipts"][0]["result"]["economyWait"]["state"] == "waiting"
    request = RoleWait(
        **binding.model_dump(), call_id="wait1", model_step=1, run_id=binding.scope.identity("agent-run", "role:0")
    )
    await save_wait(root.store, request, operation_identity(binding.job, binding.scope.epoch, 1, "wait1"))
    return request


def test_handoff_can_read_finished_main_after_a_restart_during_another_role(tmp_path, monkeypatch):
    async def run():
        monkeypatch.setattr(TurnRoles, "synchronize_war", AsyncMock())
        monkeypatch.setattr(TurnRoles, "synchronize_economy", AsyncMock())
        ledger = Ledger()
        root, store, handoffs, ticket, binding, _ = await setup(tmp_path, ledger)
        try:
            assert root.turn_roles is not None and root.record is not None
            await root.put("nativeRoleTurn:" + binding.job, ticket.scope.activation)
            await root.registry(binding, "finish1", 1, {"kind": "finish", "arguments": {}})
            root.turn_roles.ready = {role: True for role in root.turn_roles.ready}
            root.turn_roles.economy_requested = root.turn_roles.military_requested = False
            root.record["requests"] = {"gameplay:0": binding.model_dump(mode="json")}
            await root.turn_roles.save()
            with root.memory.transaction() as db:
                db.execute(tables.jobs.update().where(tables.jobs.c.id == binding.job).values(status="INTERRUPTED"))
            request = NativeHandoffRequest(ticket=ticket, attempt=0)
            effect = (await handoffs.prepare(request)).effect
            assert effect is not None
            receipt = await handoffs.execute(effect)
            result = await handoffs.apply(NativeHandoffApplication(effect=effect, receipt=receipt))
            assert await handoffs.finish(effect) == result
            assert result.committed and root.registry.load(binding).retired
            assert (await handoffs.prepare(request)).result == result and ledger.effects == 1
            with root.memory.transaction() as db:
                assert (
                    db.scalar(
                        tables.jobs.select()
                        .with_only_columns(tables.jobs.c.status)
                        .where(tables.jobs.c.id == binding.job)
                    )
                    == "COMPLETED"
                )
            assert any(name == "player_turn" for name, _ in ledger.calls)
        finally:
            await root.close()
            store.close()

    asyncio.run(run())


def test_preparation_retry_does_not_complete_its_unadmitted_job(tmp_path, monkeypatch):
    from civ_agent.startup_context import prepare_startup

    async def run():
        root, store, _ = await runtime(tmp_path)
        try:
            assert root.record is not None
            root.record.update(bootstraps={}, requests={}, initialized=True)
            root.game_state["request"] = {
                "selection": ModelSelection(model="test").model_dump(mode="json"),
                "request_limit": 24,
            }
            ticket = TurnTicket.model_validate(root.record["ticket"])
            opened = []

            async def interrupt(gateway, job, **kwargs):
                opened.append(job.id)
                if len(opened) == 1:
                    raise OSError("interrupted after opening the job")
                return await prepare_startup(gateway, job, **kwargs)

            monkeypatch.setattr("civ_agent.orchestration.coordinator.prepare_startup", interrupt)
            request = PrepareRole(ticket=ticket, purpose="economy")
            with pytest.raises(OSError):
                await root.prepare_role(request)
            prepared = await root.prepare_role(request)
            assert prepared is not None and opened == [prepared.job, prepared.job]
            with root.memory.transaction() as db:
                assert (
                    db.scalar(
                        tables.jobs.select()
                        .with_only_columns(tables.jobs.c.status)
                        .where(tables.jobs.c.id == prepared.job)
                    )
                    == "RUNNING"
                )
        finally:
            await root.close()
            store.close()

    asyncio.run(run())
