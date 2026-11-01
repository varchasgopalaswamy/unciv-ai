import asyncio
import json
from contextlib import asynccontextmanager

import pytest

from civ_agent.civilization_state import CivilizationState
from civ_agent.proposal_gateway import ProposalGateway
from civ_agent.service_io import ServiceIO
from tests.test_agent_service import AgentService, FakeFactory
from tests.test_proposal_gateway import Host

MOVE = {"kind": "MOVE_UNIT", "target": "unit1", "destination": {"x": 1, "y": 0}}


@asynccontextmanager
async def session(tmp_path, host):
    memory = CivilizationState(tmp_path / "private", "Rome")
    io = ServiceIO()
    gateway = ProposalGateway(memory, io, tmp_path / "workspace")
    try:
        job = await gateway.open("game", "gameplay", "g1", host, {}, 0)
        await gateway.call(job, "player_turn", {}, "start")
        chat = await gateway.open("chat", "communication", "g1", host, {}, 0)
        yield gateway, memory, job, chat
    finally:
        await io.close()


@pytest.mark.parametrize(
    "action",
    [
        MOVE,
        {"kind": "SET_UNIT_ORDER", "target": "unit1", "selection": "EXPLORE"},
        {"kind": "QUEUE_CONSTRUCTION", "target": "city1", "selection": "Granary"},
    ],
)
def test_cache_changes_do_not_block_routine_actions_or_require_annotations(tmp_path, action):
    async def run():
        host = Host()
        async with session(tmp_path, host) as (gateway, memory, job, chat):
            for index in range(3):
                # Another job refreshes a shared record while this model thinks.
                await gateway.io.call(memory.observe, "unit/unit1", {"turn": index, "health": 100}, chat.id)
                await gateway.io.call(memory.observe, "city/city1", {"turn": index}, chat.id)
                result = await gateway.call(job, "player_command", action, str(index))
                assert result["done"] and "error" not in result
                assert await gateway.call(job, "player_command", action, str(index)) == result
                assert len(host.executed) == index + 1
            assert not gateway.failed.is_set() and memory.run_failure() is None
            assert not memory.pending()
            assert "validationVersions" not in memory.snapshot("gameplay")

    asyncio.run(run())


def test_host_revision_rejection_survives_removal_of_local_cache_checks(tmp_path):
    class RevisionHost(Host):
        async def tool(self, name, args):
            if name == "player_command" and args["revision"] != self.revision:
                return {"error": "STALE_REVISION", "applied": False}
            return await super().tool(name, args)

    async def run():
        host = RevisionHost()
        async with session(tmp_path, host) as (gateway, memory, job, chat):
            host.revision = "changed-while-thinking"
            await gateway.call(chat, "player_turn", {}, "other-job-read")
            for call_id in ("first", "second"):
                result = await gateway.call(job, "player_command", MOVE, call_id)
                assert result == {"error": "STALE_REVISION", "applied": False}
            assert not host.executed and memory.run_failure() is None
            await gateway.call(job, "player_turn", {}, "refresh")
            assert (await gateway.call(job, "player_command", MOVE, "fresh"))["done"]
            assert host.executed[0]["revision"] == "changed-while-thinking"

    asyncio.run(run())


def test_turn_only_cache_refresh_does_not_block_an_unannotated_trade_offer(tmp_path):
    """Game 3's communication job inherited an unseen diplomacy-cache version."""

    async def run():
        host = Host()
        host.view["turn"] = 10
        host.view["diplomacy"] = {"knownCivilizations": [{"civilizationId": "Greece", "atWar": False}]}
        async with session(tmp_path, host) as (gateway, memory, game, chat):
            assert "observation:diplomacy/Greece" not in chat.memory["records"]
            await gateway.call(chat, "trade_options", {"partner": "Greece"}, "fresh-options")
            host.view["turn"] = 11
            await gateway.call(game, "player_turn", {}, "turn-refresh")
            offer = {
                "partner": "Greece",
                "give": [{"type": "Luxury_Resource", "name": "Silver", "amount": 1, "duration": 30}],
                "receive": [{"type": "Gold", "name": "Gold", "amount": 75, "duration": -1}],
            }
            result = await gateway.call(chat, "trade_offer", offer, "offer")
            assert result["sent"] and "error" not in result
            assert await gateway.call(chat, "trade_offer", offer, "offer") == result
            assert memory.run_failure() is None and not memory.pending()

    asyncio.run(run())


def test_service_stops_running_gameplay_and_communication_without_model_cooperation(tmp_path):
    class ChatProcess:
        closed = False

        async def start(self, token, settings):
            pass

        async def request(self, method, payload):
            return {
                "thread": {"id": "chat-thread"},
                "turn": {"id": "chat-turn"},
                "model": "test",
                "modelProvider": "openai",
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
            service.chat.settings = dict(service.default_settings)
            chat_task = asyncio.create_task(service.chat.compute("chat"))
            async with asyncio.timeout(3):
                # Exercise a running conversation; job registration precedes
                # startup reads and can otherwise race the injected store failure.
                while (
                    "chat" not in service.chat.job_records or service.chat.job_records["chat"][0]["phase"] != "RUNNING"
                ):
                    await asyncio.sleep(0.01)
            await service.gateway.call(game, "player_turn", {}, "failure-source")
            await service.io.call(
                service.memory.fail_run,
                game.id,
                "failure-source",
                "STATE_STORE_FAILED",
                {"exceptionType": "OSError"},
                {"applied": False},
            )
            await service.gateway.sync_failure()
            await asyncio.wait_for(chat_task, 3)
            assert service.active_task is not None
            await asyncio.wait_for(asyncio.shield(service.active_task), 3)
            assert all(process.closed for process in factory.instances)
            assert service.status()["phase"] == "FAILED" and service.status()["error"] == "STATE_STORE_FAILED"
            chat_record = json.loads(next((service.state_dir / "chat-jobs").glob("*.json")).read_text())
            assert chat_record["phase"] == "FAILED" and chat_record["error"] == "STATE_STORE_FAILED"
            failure = service.health()["runFailure"]
            assert failure is not None and failure["job"] == game.id
        finally:
            if chat_task and not chat_task.done():
                chat_task.cancel()
                await asyncio.gather(chat_task, return_exceptions=True)
            await service.close()

    asyncio.run(run())
