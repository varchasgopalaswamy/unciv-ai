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


class AirHost(Host):
    async def tool(self, name, args):
        if name == "mission_preview":
            return {
                "epoch": "g1",
                "revision": self.revision,
                "turn": 0,
                **args,
                "available": args["destination"]["x"] >= 0,
                "confirmation": "Launch with native consequences?" if args["mission"] == "NUCLEAR_STRIKE" else None,
            }
        return await super().tool(name, args)


@pytest.mark.parametrize("kind", ["REBASE_UNIT", "PARADROP", "AIR_SWEEP", "NUCLEAR_STRIKE"])
def test_mission_requires_exact_current_preview_and_invalidates_memory(tmp_path, kind):
    async def run():
        io = ServiceIO()
        state = CivilizationState(tmp_path / "state", "Rome")
        gateway = ProposalGateway(state, io, tmp_path / "workspace")
        host = AirHost()
        job = await gateway.open("game", "gameplay", "g1", host, {})
        action = {
            "kind": kind,
            "target": "unit1",
            "destination": {"x": 4, "y": 0},
            "intent": {"reason": "Complete the selected native mission"},
        }
        if kind == "NUCLEAR_STRIKE":
            action["confirmAction"] = True
        query = {
            "target": "unit1",
            "mission": "REBASE" if kind == "REBASE_UNIT" else kind,
            "destination": action["destination"],
        }
        assert Draft202012Validator(command_schema()).is_valid(action)
        assert command_arguments(action) == action
        detail = required_detail(action)
        assert detail is not None and detail.arguments == query
        with pytest.raises(ValidationError):
            command_arguments({**action, "attackFrom": {"x": 0, "y": 0}})
        try:
            await gateway.call(job, "player_turn", {}, "read")
            await gateway.call(job, "unit_details", {"target": "unit1"}, "unit")
            missing = await gateway.call(job, "player_command", action, "no-preview")
            assert missing["error"] == "DETAIL_REQUIRED"
            assert missing["recovery"]["reads"] == [{"tool": "mission_preview", "arguments": query}]
            assert not host.executed
            preview = await gateway.call(job, "mission_preview", query, "preview")
            assert preview["available"] and "revision" not in preview
            wrong = {**action, "destination": {"x": 5, "y": 0}}
            assert (await gateway.call(job, "player_command", wrong, "wrong"))["error"] == "DETAIL_REQUIRED"
            await gateway.call(job, "player_turn", {}, "same-view")
            assert job.memory["records"][preview["evidence"]]["body"]["available"]
            receipt = await gateway.call(job, "player_command", action, "act")
            assert host.executed[-1]["kind"] == kind
            assert await gateway.call(job, "player_command", action, "act") == receipt
            await gateway.call(job, "player_turn", {}, "changed")
            assert not job.memory["records"][preview["evidence"]]["body"]["available"]
            assert (await gateway.call(job, "player_command", action, "stale"))["error"] == "DETAIL_REQUIRED"
            host.revision = "rev99"
            assert (await gateway.call(job, "mission_preview", query, "changed-preview"))[
                "error"
            ] == "OBSERVATION_REQUIRED"
            assert not job.details_read
        finally:
            await gateway.close(job, True)
            await io.close()

    asyncio.run(run())


def test_compact_roster_exposes_air_missions_transport_and_automatic_interception():
    host = AirHost()
    host.view["phase"] = "ACTIVE"
    host.view["map"]["units"][0]["air"] = {
        "missions": [{"mission": "REBASE", "available": True}, {"mission": "AIR_SWEEP", "available": False}],
        "transported": True,
        "carriedUnits": [],
        "interception": {"ready": True},
    }
    shown = overview(host.view)["units"][0]
    assert shown["missions"] == ["REBASE"]
    assert shown["transported"] and shown["interceptionReady"]
