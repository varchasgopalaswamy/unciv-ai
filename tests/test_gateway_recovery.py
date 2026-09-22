import asyncio
import json
from types import SimpleNamespace

import pytest

from civ_agent.chat_runtime import ChatRuntime
from civ_agent.civilization_state import CivilizationState, StateConflict
from civ_agent.proposal_gateway import ProposalGateway
from civ_agent.service_io import ServiceIO
from tests.test_proposal_gateway import Host


@pytest.mark.parametrize("enacted", [False, True])
def test_lost_response_is_reconciled_without_executing_another_action(tmp_path, enacted):
    async def run():
        memory = CivilizationState(tmp_path / "private", "Rome")
        io = ServiceIO()
        gateway = ProposalGateway(memory, io, tmp_path / "workspace")
        host = Host()
        original = host.tool
        interrupted = False

        async def unreliable(name, args):
            nonlocal interrupted
            payload = args
            if name == "player_receipt":
                return host.seen.get(payload["requestId"], {"error": "NOT_ENACTED"})
            if name == "player_command" and not interrupted:
                interrupted = True
                if enacted:
                    await original(name, payload)
                raise TimeoutError("Response lost")
            return await original(name, payload)

        host.tool = unreliable
        try:
            job = await gateway.open("old", "gameplay", "g1", host, {}, 0)
            await gateway.call(job, "player_turn", {}, "observe")
            await gateway.call(job, "unit_details", {"target": "unit1"}, "inspect")
            with pytest.raises(TimeoutError):
                await gateway.call(
                    job,
                    "player_command",
                    {"kind": "MOVE_UNIT", "target": "unit1", "destination": {"x": 1, "y": 0}},
                    "action",
                )
            with pytest.raises(StateConflict):
                await gateway.call(
                    job,
                    "player_command",
                    {"kind": "MOVE_UNIT", "target": "unit1", "destination": {"x": 1, "y": 0}},
                    "new-id",
                )
            pending = memory.pending()[0]
            restarted = CivilizationState(tmp_path / "private", "Rome")
            runtime = ChatRuntime(SimpleNamespace(io=io, memory=restarted))
            runtime.connection, runtime.epoch = host, "g1"
            await runtime.reconcile()
            assert not restarted.pending()
            assert len(host.executed) == int(enacted)
            row = restarted.requests_for_job("old")[0]
            assert row["id"] == pending["id"]
            assert json.loads(row["result"]).get("done", False) == enacted
        finally:
            await io.close()

    asyncio.run(run())


def test_receipt_survives_interruption_between_host_commit_and_mcp_response_cache(tmp_path):
    async def run():
        memory = CivilizationState(tmp_path / "private", "Rome")
        io = ServiceIO()
        gateway = ProposalGateway(memory, io, tmp_path / "workspace")
        host = Host()
        try:
            job = await gateway.open("job", "gameplay", "g1", host, {})
            await gateway.call(job, "player_turn", {}, "read")
            args = {"kind": "MOVE_UNIT", "target": "unit1", "destination": {"x": 1, "y": 0}}
            memory.call("job", "move", "player_command", args)
            queued = memory.queue("job", "player_command", {**args, "epoch": "g1", "revision": "rev1"}, {}, "move")
            memory.receipt(queued["id"], await host.tool("player_command", json.loads(queued["payload"])))
            job.observation = None
            assert (await gateway.call(job, "player_command", args, "move"))["done"]
            assert len(host.executed) == 1
        finally:
            await io.close()

    asyncio.run(run())


def test_checkpoint_checks_database_identity_and_referenced_evidence(tmp_path):
    memory = CivilizationState(tmp_path / "private", "Rome")
    memory.bind("g1")
    memory.observe("game", {"turn": 0, "city": "Rome"})
    path = tmp_path / "round.json"
    checksum = memory.backup(path)
    unrelated = CivilizationState(tmp_path / "other", "Rome")
    with pytest.raises(StateConflict, match="CHECKPOINT_INVALID"):
        unrelated.verify(path, checksum)
    with memory.transaction() as db:
        db.exec_driver_sql("UPDATE records SET body='{}'")
    with pytest.raises(StateConflict, match="CHECKPOINT_HISTORY_CHANGED"):
        memory.verify(path, checksum)


def test_new_private_policy_requires_reconsideration_but_own_updates_are_immediately_usable(tmp_path):
    async def run():
        memory = CivilizationState(tmp_path / "private", "Rome")
        io = ServiceIO()
        gateway = ProposalGateway(memory, io, tmp_path / "workspace")
        host = Host()
        try:
            planning = await gateway.open("planning", "gameplay", "g1", host, {})
            chat = await gateway.open("chat", "communication", "g1", host, {})
            await gateway.call(planning, "player_turn", {}, "observe")
            await gateway.call(planning, "unit_details", {"target": "unit1"}, "inspect")
            change = {
                "op": "create",
                "content": {
                    "type": "plan",
                    "objective": "Hold defensive positions",
                    "approach": "Carry out the stated objective",
                    "successCriteria": "Objective achieved",
                },
            }
            await gateway.call(chat, "memory_update", {"changes": [change]}, "change")
            stale = await gateway.call(
                planning,
                "player_command",
                {"kind": "MOVE_UNIT", "target": "unit1", "destination": {"x": 1, "y": 0}, "intent": {"plan": "mem1"}},
                "old-plan",
            )
            assert stale["error"] == "STALE_ASSUMPTIONS" and stale["applied"] is False and stale["recoverable"]
            assert stale["changedRecords"] == ["mem1"]
            assert stale["recovery"]["reads"] == [
                {"tool": "memory_read", "arguments": {"ids": ["mem1"], "includeRetired": True}}
            ]
            assert not memory.requests_for_job(planning.id) and not host.executed
            await gateway.call(planning, "memory_read", {"ids": ["mem1"], "includeRetired": True}, "reconsider")
            change["op"], change["id"] = "revise", "mem1"
            change["content"]["objective"] = "Move back to defend the capital"
            await gateway.call(planning, "memory_update", {"changes": [change]}, "new-plan")
            assert (
                await gateway.call(
                    planning,
                    "player_command",
                    {
                        "kind": "MOVE_UNIT",
                        "target": "unit1",
                        "destination": {"x": 1, "y": 0},
                        "intent": {"plan": "mem1"},
                    },
                    "old-plan",
                )
                == stale
            )  # A transport replay never executes the obsolete decision after refresh.
            assert not host.executed
            assert (
                await gateway.call(
                    planning,
                    "player_command",
                    {"kind": "MOVE_UNIT", "target": "unit1", "destination": {"x": 1, "y": 0}},
                    "defend",
                )
            )["done"]
        finally:
            await io.close()

    asyncio.run(run())
