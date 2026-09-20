import asyncio

import pytest
from jsonschema import Draft202012Validator
from pydantic import ValidationError

from civ_agent.civilization_state import CivilizationState
from civ_agent.game_commands import command_arguments, command_schema
from civ_agent.observations import overview, required_detail
from civ_agent.proposal_gateway import ProposalGateway
from civ_agent.service_io import ServiceIO
from tests.test_proposal_gateway import Host


@pytest.mark.parametrize(
    "command,read",
    [
        ({"kind": "SET_UNIT_DESTINATION", "target": "unit1", "destination": {"x": 8, "y": 0}}, "movement_preview"),
        ({"kind": "CANCEL_UNIT_MOVEMENT", "target": "unit1"}, "unit_details"),
        ({"kind": "CREATE_WATER_IMPROVEMENT", "target": "unit1", "selection": "Fishing Boats"}, "unit_details"),
    ],
)
def test_naval_commands_have_explicit_shapes_and_offer_reads(command, read):
    assert Draft202012Validator(command_schema()).is_valid(command)
    assert command_arguments(command) == command
    detail = required_detail(command)
    assert detail is not None and detail.tool == read
    with pytest.raises(ValidationError):
        command_arguments({**command, "attackFrom": {"x": 0, "y": 0}})


class MovementHost(Host):
    async def tool(self, name, args):
        if name == "movement_preview":
            return {
                "epoch": "g1",
                "revision": self.revision,
                "turn": 0,
                **args,
                "available": args["destination"]["x"] >= 0,
                "estimatedTurns": 4,
                "turns": [{"destination": args["destination"], "path": [args["destination"]], "speculative": True}],
            }
        return await super().tool(name, args)


def test_distant_order_requires_exact_current_preview_and_invalidates_memory_on_change(tmp_path):
    async def run():
        io = ServiceIO()
        state = CivilizationState(tmp_path / "state", "Rome")
        gateway = ProposalGateway(state, io, tmp_path / "workspace")
        host = MovementHost()
        job = await gateway.open("game", "gameplay", "g1", host, {})
        query = {"target": "unit1", "destination": {"x": 8, "y": 0}}
        action = {"kind": "SET_UNIT_DESTINATION", **query, "intent": {"reason": "Sail to the chosen island"}}
        try:
            await gateway.call(job, "player_turn", {}, "read")
            await gateway.call(job, "unit_details", {"target": "unit1"}, "unit")
            missing = await gateway.call(job, "player_command", action, "no-preview")
            assert missing["error"] == "DETAIL_REQUIRED"
            assert missing["recovery"]["reads"] == [{"tool": "movement_preview", "arguments": query}]
            assert not host.executed
            preview = await gateway.call(job, "movement_preview", query, "preview")
            assert preview["available"] and "revision" not in preview
            assert preview["evidence"] in job.memory["records"]
            elsewhere = {**action, "destination": {"x": 9, "y": 0}}
            assert (await gateway.call(job, "player_command", elsewhere, "wrong-route"))["error"] == "DETAIL_REQUIRED"
            await gateway.call(job, "player_turn", {}, "same-view")
            assert job.memory["records"][preview["evidence"]]["body"]["available"]
            receipt = await gateway.call(job, "player_command", action, "move")
            assert host.executed[-1]["destination"] == query["destination"]
            assert await gateway.call(job, "player_command", action, "move") == receipt
            await gateway.call(job, "player_turn", {}, "changed-view")
            assert not job.memory["records"][preview["evidence"]]["body"]["available"]
            assert (await gateway.call(job, "player_command", action, "old-preview"))["error"] == "DETAIL_REQUIRED"
            host.revision = "rev99"
            stale = await gateway.call(job, "movement_preview", query, "stale-read")
            assert stale["error"] == "OBSERVATION_REQUIRED"
            assert not job.details_read
            await gateway.call(job, "player_turn", {}, "new-view")
            unavailable = {"target": "unit1", "destination": {"x": -1, "y": 0}}
            assert not (await gateway.call(job, "movement_preview", unavailable, "unavailable"))["available"]
            assert (await gateway.call(job, "player_command", {**action, **unavailable}, "blocked"))[
                "error"
            ] == "DETAIL_REQUIRED"
        finally:
            await gateway.close(job, True)
            await io.close()

    asyncio.run(run())


def test_overview_exposes_destination_progress_and_work_boat_offer():
    host = MovementHost()
    host.view["map"]["units"][0]["movementOrder"] = {"destination": {"x": 8, "y": 0}, "canCancel": True}
    host.view["map"]["units"][0]["waterImprovement"] = {"name": "Fishing Boats", "available": True}
    host.view["phase"] = "ACTIVE"
    shown = overview(host.view)["units"][0]
    assert shown["movementOrder"] == host.view["map"]["units"][0]["movementOrder"]
    assert shown["canCreateWaterImprovement"]
