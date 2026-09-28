import asyncio
import json
from contextlib import asynccontextmanager

import pytest

from civ_agent.civilization_state import CivilizationState, StateConflict
from civ_agent.proposal_gateway import ProposalGateway
from civ_agent.service_io import ServiceIO
from civ_agent.stale_recovery import REPEATED_STALE
from civ_agent.tool_discovery import tool_discovery_instructions
from tests.test_agent_service import AgentService, FakeFactory
from tests.test_proposal_gateway import Host

PLAN = {
    "type": "strategy",
    "assessment": "Defend Rome",
    "reasons": ["Hold the hill"],
    "reconsiderWhen": ["Rome survives"],
}
MOVE = {"kind": "MOVE_UNIT", "target": "unit1", "destination": {"x": 1, "y": 0}}


@asynccontextmanager
async def session(tmp_path):
    memory = CivilizationState(tmp_path / "private", "Rome")
    io = ServiceIO()
    gateway = ProposalGateway(memory, io, tmp_path / "workspace")
    host = Host()
    try:
        job = await gateway.open("game", "gameplay", "g1", host, {}, 0)
        await gateway.call(job, "player_turn", {}, "start")
        await gateway.call(job, "unit_details", {"target": "unit1"}, "inspect")
        result = await gateway.call(job, "memory_update", {"changes": [{"op": "create", "content": PLAN}]}, "strategy")
        key = result["changes"][0]["id"]
        chat = await gateway.open("chat", "communication", "g1", host, {}, 0)
        yield gateway, memory, job, chat, host, key
    finally:
        await io.close()


def change_dependency(memory, chat, key, text):
    # Simulate a newer player-visible unit observation arriving from another job.
    memory.observe("unit/unit1", {"turn": 0, "entity": {"handle": "unit1", "status": text}}, chat.id)


async def recover(gateway, job, result, prefix):
    responses = []
    for index, read in enumerate(result["recovery"]["reads"]):
        responses.append(await gateway.call(job, read["tool"], read["arguments"], f"{prefix}:{index}"))
    return responses


def test_repeated_action_conflict_stops_all_jobs_and_survives_restart(tmp_path):
    async def run():
        async with session(tmp_path) as (gateway, memory, job, chat, host, key):
            action = {**MOVE, "intent": {"strategy": key, "reason": "Defend Rome"}}
            change_dependency(memory, chat, key, "Defend the city instead")
            first = await gateway.call(job, "player_command", action, "first")
            assert first["error"] == "STALE_ASSUMPTIONS" and first["recoverable"]
            assert not first["applied"]
            await recover(gateway, job, first, "refresh")
            assert await gateway.call(job, "player_command", action, "first") == first
            assert not gateway.failed.is_set()
            change_dependency(memory, chat, key, "Reinforce the frontier")
            second = await gateway.call(job, "player_command", action, "second")
            assert second["error"] == REPEATED_STALE and second["recoverable"] is False
            assert not second["applied"] and not host.executed and not memory.pending()
            assert gateway.failed.is_set()
            for blocked in (job, chat):
                with pytest.raises(StateConflict, match=REPEATED_STALE):
                    await gateway.call(blocked, "chat_send", {"to": "Greece", "text": "Continue"}, "after")
            failure = memory.run_failure()
            assert failure["firstAttempt"]["callId"] == "first"
            assert failure["secondAttempt"]["callId"] == "second"
            assert failure["secondAttempt"]["conflicts"][0]["changedByJob"] == "chat"
            assert (
                failure["secondAttempt"]["conflicts"][0]["expectedVersion"]
                < failure["secondAttempt"]["conflicts"][0]["currentVersion"]
            )
            assert failure["interveningReads"][0]["tool"] == "player_turn"
            assert json.loads((memory.root / "failures" / (failure["diagnosticId"] + ".json")).read_text()) == failure
            assert memory.call(job.id, "second", "player_command", action) == second
            restarted = CivilizationState(memory.root, "Rome")
            next_gateway = ProposalGateway(restarted, gateway.io, tmp_path / "workspace")
            with pytest.raises(StateConflict, match=REPEATED_STALE):
                await next_gateway.open("new", "gameplay", "g1", host, {}, 0)
            await next_gateway.resume_run()
            resumed = await next_gateway.open("resumed", "gameplay", "g1", host, {}, 0)
            assert not next_gateway.failed.is_set()
            await next_gateway.close(resumed, False)

    asyncio.run(run())


def test_successful_retry_clears_conflict_and_different_operations_are_independent(tmp_path):
    async def run():
        async with session(tmp_path) as (gateway, memory, job, chat, host, key):
            action = {**MOVE, "intent": {"strategy": key, "reason": "Defend Rome"}}
            change_dependency(memory, chat, key, "Updated defensive position")
            first = await gateway.call(job, "player_command", action, "first")
            other = {**action, "destination": {"x": 2, "y": 0}}
            assert (await gateway.call(job, "player_command", other, "other"))["error"] == "STALE_ASSUMPTIONS"
            await recover(gateway, job, first, "read")
            assert (await gateway.call(job, "player_command", action, "success"))["done"]
            await gateway.call(job, "unit_details", {"target": "unit1"}, "inspect-again")
            change_dependency(memory, chat, key, "Another update after successful movement")
            assert (await gateway.call(job, "player_command", action, "later"))["error"] == "STALE_ASSUMPTIONS"
            assert not gateway.failed.is_set() and len(host.executed) == 1

    asyncio.run(run())


def test_recovery_refreshes_explicit_detail_assumptions_before_the_fresh_attempt(tmp_path):
    async def run():
        async with session(tmp_path) as (gateway, _, job, chat, host, _):
            details = await gateway.call(job, "unit_details", {"target": "unit1"}, "source")
            action = {
                **MOVE,
                "intent": {
                    "reason": "Move this warrior",
                    "assumptions": [{"text": "The warrior is healthy", "source": details["evidence"]}],
                },
            }
            host.view["map"]["units"][0]["health"] = 90
            host.revision = "changed"
            await gateway.call(chat, "player_turn", {}, "concurrent-view")
            first = await gateway.call(job, "player_command", action, "first")
            assert first["error"] == "STALE_ASSUMPTIONS"
            assert any(read["tool"] == "unit_details" for read in first["recovery"]["reads"])
            refreshed = await recover(gateway, job, first, "refresh")
            source = next(
                value["evidence"] for value in refreshed if value.get("query", {}).get("tool") == "unit_details"
            )
            assert source != details["evidence"]
            action["intent"]["assumptions"][0]["source"] = source
            assert (await gateway.call(job, "player_command", action, "fresh"))["done"]
            assert len(host.executed) == 1 and not gateway.failed.is_set()

    asyncio.run(run())


@pytest.mark.parametrize("purpose", ["gameplay", "communication"])
def test_prompts_require_bounded_recovery(purpose):
    instructions = tool_discovery_instructions(purpose)
    assert "recoverable=false is terminal: stop without further tool calls" in instructions
    assert "Follow recoverable error responses" in instructions
    assert "do not stop the gameplay turn for this" not in instructions


def test_service_stops_running_gameplay_and_communication_without_model_cooperation(tmp_path):
    class ChatProcess:
        closed = False

        async def start(self, token, model):
            pass

        async def request(self, method, payload):
            return {
                "thread": {"id": "chat-thread"},
                "turn": {"id": "chat-turn"},
                "model": "test",
                "reasoningEffort": "medium",
            }

        async def close(self):
            self.closed = True

    class Factory(FakeFactory):
        def __call__(self, executable, workspace, mcp_url, ca_file, notification):
            if workspace.name != "chat":
                return super().__call__(executable, workspace, mcp_url, ca_file, notification)
            process = ChatProcess()
            self.instances.append(process)
            return process

    async def run():
        factory = Factory()
        factory.complete = False
        service = AgentService(
            "Rome",
            tmp_path / "workspace",
            tmp_path / "service",
            "S" * 43,
            "https://localhost:8765/mcp",
            None,
            process_factory=factory,
            connection_factory=factory.connection,
        )
        factory.service = service
        chat_task = None
        try:
            await service.start(
                {
                    "activationId": "game",
                    "epoch": "g1",
                    "gameTurn": 0,
                    "handoff": 0,
                    "civilization": "Rome",
                    "threadId": None,
                    "mcpUrl": service.mcp_url,
                    "playerToken": "P" * 43,
                    "prompt": "Play",
                    "model": None,
                    "timeoutSeconds": 20,
                    "leaseSeconds": 10,
                }
            )
            async with asyncio.timeout(3):
                while service.status()["phase"] != "RUNNING":
                    await asyncio.sleep(0.01)
            game = next(job for job in service.gateway.jobs.values() if job.purpose == "gameplay")
            original_close = factory.instances[0].close

            async def interrupted_close():
                await service._notification(
                    "turn/completed",
                    {
                        "threadId": service.status()["threadId"],
                        "turn": {"id": service.turn_id, "status": "interrupted"},
                    },
                )
                await original_close()

            factory.instances[0].close = interrupted_close
            original_tool = game.connection.tool

            async def with_job_reports(name, arguments):
                if name == "player_communication" and arguments.get("op", "").startswith("job_"):
                    return {"granted": True}
                return await original_tool(name, arguments)

            game.connection.tool = with_job_reports
            service.chat.connection = game.connection
            service.chat.epoch = "g1"
            chat_task = asyncio.create_task(service.chat.compute("chat"))
            async with asyncio.timeout(3):
                while "chat" not in service.chat.jobs:
                    await asyncio.sleep(0.01)
            chat = service.chat.jobs["chat"][0]
            await service.gateway.call(game, "unit_details", {"target": "unit1"}, "inspect")
            change_dependency(service.memory, chat, None, "Concurrent unit observation")
            first = await service.gateway.call(game, "player_command", MOVE, "first")
            assert first["error"] == "STALE_ASSUMPTIONS"
            second = await service.gateway.call(game, "player_command", MOVE, "second")
            assert second["error"] == REPEATED_STALE
            await asyncio.wait_for(chat_task, 3)
            assert service.active_task is not None
            await asyncio.wait_for(asyncio.shield(service.active_task), 3)
            assert all(process.closed for process in factory.instances)
            assert service.status()["phase"] == "FAILED" and service.status()["error"] == REPEATED_STALE
            chat_record = json.loads(next((service.state_dir / "chat-jobs").glob("*.json")).read_text())
            assert chat_record["phase"] == "FAILED" and chat_record["error"] == REPEATED_STALE
            failure = service.health()["runFailure"]
            assert failure is not None and failure["job"] == game.id
        finally:
            if chat_task and not chat_task.done():
                chat_task.cancel()
                await asyncio.gather(chat_task, return_exceptions=True)
            await service.close()

    asyncio.run(run())
