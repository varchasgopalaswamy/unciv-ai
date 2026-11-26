import asyncio

import pytest

from civ_agent.civilization_state import StateConflict
from civ_agent.decision_contracts import DecisionContract
from civ_agent.gateway_batches import BatchExecutor
from civ_agent.orchestration.gateway_execution import NativeGatewayExecutor
from civ_agent.orchestration.gateway_state import capture_job, restore_job
from civ_agent.orchestration.store import JournalConflict
from tests.domain_fixtures import command, managed, run_async


async def executor(m, job=None):
    job = job or m.economy
    await m.gateway.call(job, "player_turn", {}, "native-start")
    contract = DecisionContract(job.purpose, await m.gateway.tools(job))
    access = BatchExecutor(m.gateway, job, contract).delivered_access
    state = capture_job(job, delivered_access=access)
    return NativeGatewayExecutor(m.gateway, job, contract, state), state


@run_async
async def test_native_commands_replay_without_reexecution_and_reject_changed_requests(tmp_path):
    async with managed(tmp_path) as m:
        runner, _ = await executor(m)
        decision = {"kind": "act", "operations": [command("SET_CITY_FOCUS", target="city2", selection="FoodFocus")]}
        first = await runner.execute("call1", decision, model_step=1)
        assert first.result["receipts"][0]["status"] == "confirmed"
        assert len(m.host.executed) == 1
        replay = await runner.execute("call1", decision, model_step=1)
        assert replay == first and len(m.host.executed) == 1
        changed = {"kind": "act", "operations": [command("SET_CITY_FOCUS", target="city2", selection="GoldFocus")]}
        with pytest.raises(JournalConflict, match="OPERATION_CHANGED"):
            await runner.execute("call1", changed, model_step=1)
        assert len(m.host.executed) == 1
        second = await runner.execute("call1", changed, model_step=2)
        assert second.result["receipts"][0]["status"] == "confirmed"
        assert len(m.host.executed) == 2
        latest = runner.capture()
        assert await runner.execute("call1", decision, model_step=1) == first
        assert runner.delivery_step == latest.delivery_step
        assert runner.capture().data == latest.data
        assert len(m.host.executed) == 2


@run_async
async def test_inspection_evidence_is_staged_until_the_next_model_step(tmp_path):
    async with managed(tmp_path) as m:
        runner, _ = await executor(m)
        query = {"tool": "city_details", "arguments": {"target": "city2"}}
        inspection = await runner.execute("read1", {"kind": "inspect", "operations": [query]}, model_step=1)
        delivered = inspection.result["receipts"][0]["result"]["query"]
        assert delivered not in inspection.state.delivered_access["details"]
        assert inspection.state.pending_access is not None
        assert delivered in inspection.state.pending_access["details"]
        sibling = NativeGatewayExecutor(m.gateway, m.economy, runner.contract, inspection.state)
        assert delivered not in sibling.loop.delivered_access["details"]
        sibling._begin_step(1)
        assert delivered not in sibling.loop.delivered_access["details"]
        sibling._begin_step(2)
        assert delivered in sibling.loop.delivered_access["details"]


@pytest.mark.parametrize("uncertain", [False, True])
@run_async
async def test_native_recovery_reconciles_receipts_and_never_sends_a_command_again(tmp_path, uncertain):
    async with managed(tmp_path) as m:
        runner, before = await executor(m)
        original = m.host.tool
        reconciliations = []

        async def lost(name, arguments):
            if name == "player_receipt":
                reconciliations.append(arguments["requestId"])
                return {"error": "NOT_ENACTED"} if uncertain else m.host.seen[arguments["requestId"]]
            value = await original(name, arguments)
            if name == "player_command":
                raise asyncio.CancelledError()
            return value

        m.host.tool = lost
        decision = {
            "kind": "act",
            "operations": [
                command("SET_CITY_FOCUS", target=city, selection="FoodFocus") for city in ("city1", "city2")
            ],
        }
        with pytest.raises(asyncio.CancelledError):
            await runner.execute("lost1", decision, model_step=1)
        restored = restore_job(before, m.host, job_id=m.economy.id, purpose=m.economy.purpose, epoch=m.economy.epoch)
        m.gateway.jobs[restored.token] = restored
        m.roles.jobs[restored.purpose] = restored
        restarted = NativeGatewayExecutor(m.gateway, restored, runner.contract, before)
        if uncertain:
            with pytest.raises(StateConflict, match="DECISION_EXECUTION_UNCERTAIN"):
                await restarted.execute("lost1", decision, model_step=1)
        else:
            recovered = await restarted.execute("lost1", decision, model_step=1)
            assert recovered.result["receipts"][0]["status"] == "confirmed"
            assert recovered.result["receipts"][1]["reason"] == "RECOVERY_REQUIRES_DECISION"
            assert recovered.result["refreshRequired"]
            assert recovered.state.data["observation"] is None
        assert len(m.host.executed) == 1 and len(reconciliations) == 1


@run_async
async def test_receipt_before_context_commit_recovers_without_promoting_an_old_view(tmp_path, monkeypatch):
    async with managed(tmp_path) as m:
        runner, before = await executor(m)
        save = runner.loop.save

        async def interrupted():
            if runner.loop.current and runner.loop.current.get("gatewayState"):
                raise OSError("worker exited before context commit")
            await save()

        monkeypatch.setattr(runner.loop, "save", interrupted)
        decision = {"kind": "act", "operations": [command("SET_CITY_FOCUS", target="city2", selection="FoodFocus")]}
        with pytest.raises(OSError):
            await runner.execute("gap1", decision, model_step=1)
        restored = restore_job(before, m.host, job_id=m.economy.id, purpose="economy", epoch="g1")
        m.gateway.jobs[restored.token] = restored
        m.roles.jobs["economy"] = restored
        recovered = await NativeGatewayExecutor(m.gateway, restored, runner.contract, before).execute(
            "gap1",
            decision,
            model_step=1,
        )
        assert recovered.result["refreshRequired"]
        assert recovered.state.delivered_access["revision"] is None
        assert recovered.state.pending_access is None
        assert len(m.host.executed) == 1


@run_async
async def test_batch_validation_precedes_every_child_and_ownership_changes_stop_siblings(tmp_path):
    async with managed(tmp_path) as m:
        runner, _ = await executor(m, m.main)
        first = {"tool": "city_override", "arguments": {"city": "city1", "reason": "Emergency"}}
        invalid = {"kind": "act", "operations": [first, {"tool": "not_a_tool", "arguments": {}}]}
        with pytest.raises(ValueError):
            await runner.execute("invalid", invalid, model_step=1)
        assert not m.roles.overrides and not m.host.executed
        valid = {
            "kind": "act",
            "operations": [
                first,
                command("QUEUE_CONSTRUCTION", target="city1", selection="Granary"),
            ],
        }
        result = await runner.execute("valid", valid, model_step=1)
        assert result.result["receipts"][0]["status"] == "confirmed"
        assert result.result["receipts"][1]["reason"] == "PREREQUISITES_CHANGED"
        assert not m.host.executed


@run_async
async def test_restored_connection_must_verify_player_identity_again(tmp_path):
    async with managed(tmp_path) as m:
        runner, before = await executor(m)
        assert m.economy.verified
        restored = restore_job(before, m.host, job_id=m.economy.id, purpose="economy", epoch="g1")
        m.gateway.jobs[restored.token] = restored
        m.roles.jobs["economy"] = restored
        original = m.host.tool

        async def wrong_player(name, arguments):
            if name == "player_communication" and arguments.get("op") == "state":
                return {"civilization": "Wrong player", "epoch": "g1"}
            return await original(name, arguments)

        m.host.tool = wrong_player
        decision = {"kind": "act", "operations": [command("SET_CITY_FOCUS", target="city2", selection="FoodFocus")]}
        with pytest.raises(StateConflict, match="PLAYER_CREDENTIAL_MISMATCH"):
            await NativeGatewayExecutor(m.gateway, restored, runner.contract, before).execute(
                "wrong", decision, model_step=1
            )
        assert not m.host.executed


@run_async
async def test_stale_epoch_is_rejected_before_dispatch_or_replay(tmp_path):
    async with managed(tmp_path) as m:
        runner, _ = await executor(m)
        decision = {"kind": "act", "operations": [command("SET_CITY_FOCUS", target="city2", selection="FoodFocus")]}
        await runner.execute("past", decision, model_step=1)
        with m.memory.transaction() as db:
            m.memory._set(db, "epoch", "g2")
        for identity in ("past", "new"):
            with pytest.raises(StateConflict, match="DECISION_CONTEXT_EXPIRED"):
                await runner.execute(identity, decision, model_step=1)
        assert len(m.host.executed) == 1
