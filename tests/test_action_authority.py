import asyncio
from contextlib import asynccontextmanager

import pytest

from civ_agent.civilization_state import CivilizationState
from civ_agent.proposal_gateway import ProposalGateway
from civ_agent.service_io import ServiceIO
from tests.domain_fixtures import Host

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
