import asyncio
import json
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from civ_agent.chat_runtime import ChatRuntime
from civ_agent.civilization_state import CivilizationState, StateConflict
from civ_agent.proposal_gateway import ProposalGateway
from civ_agent.service_io import ServiceIO
from tests.test_proposal_gateway import Host
from tests.test_structured_memory import current, replacement


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


def test_communication_contributions_do_not_gate_commands_or_modify_plans(tmp_path):
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
                    "type": "strategy",
                    "assessment": "Hold defensive positions",
                    "reasons": ["Carry out the stated assessment"],
                    "reconsiderWhen": ["Objective achieved"],
                },
            }
            strategy = await gateway.call(planning, "memory_update", {"changes": [change]}, "strategy")
            key = strategy["changes"][0]["id"]
            contributed = await gateway.call(chat, "memory_update", {"changes": [change]}, "contribute")
            assert contributed["changes"][0]["contribution"]
            assert not (await gateway.call(planning, "memory_search", {"type": "contribution"}, "inbox"))["records"]
            action = {
                "kind": "MOVE_UNIT",
                "target": "unit1",
                "destination": {"x": 1, "y": 0},
                "intent": {"strategy": key, "reason": "Defend Rome"},
            }
            moved = await gateway.call(planning, "player_command", action, "move")
            assert moved["done"] and len(host.executed) == 1
            changed = await gateway.call(
                planning,
                "memory_update",
                {"changes": [replacement(memory, key, assessment="Defend the capital")]},
                "amend",
            )
            assert changed["changes"][0]["accepted"]
            assert await gateway.call(planning, "player_command", action, "move") == moved
            assert len(host.executed) == 1
            assert memory.run_failure() is None
        finally:
            await io.close()

    asyncio.run(run())


def test_search_does_not_silently_deliver_notices_that_arrived_after_its_result(tmp_path):
    async def run():
        memory = CivilizationState(tmp_path / "private", "Rome")
        io = ServiceIO()
        gateway = ProposalGateway(memory, io, tmp_path / "workspace")
        try:
            job = await gateway.open("game", "gameplay", "g1", Host(), {})
            result = await gateway.call(
                job,
                "memory_update",
                {
                    "changes": [
                        {
                            "op": "create",
                            "content": {
                                "type": "strategy",
                                "assessment": "Defend Rome",
                                "reasons": ["Guard the border"],
                                "reconsiderWhen": ["Rome survives"],
                                "subjects": ["Greece"],
                            },
                        }
                    ]
                },
                "create",
            )
            key = result["changes"][0]["id"]
            search = memory.search_memory

            def then_receive_news(*args):
                result = search(*args)
                memory.ingest("g1", [{"id": 1, "kind": "diplomacy", "body": {"author": "Rome", "target": "Greece"}}], 1)
                return result

            with patch.object(memory, "search_memory", side_effect=then_receive_news):
                found = await gateway.call(job, "memory_search", {"type": "strategy"}, "search")
            assert not found["records"][0]["needsReview"]
            revised = await gateway.call(
                job,
                "memory_update",
                {"changes": [replacement(memory, key, reasons=["Bring reinforcements"])]},
                "amend",
            )
            assert revised["changes"][0]["record"]["needsReview"]
            assert current(memory, key)["needsReview"]
        finally:
            await io.close()

    asyncio.run(run())
