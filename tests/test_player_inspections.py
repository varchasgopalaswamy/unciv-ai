"""New reads and controls must survive model projection and observation refresh."""

import asyncio

import pytest
from pydantic import ValidationError

from civ_agent.action_templates import add_decision_commands
from civ_agent.civilization_state import CivilizationState
from civ_agent.game_commands import command_arguments
from civ_agent.observations import DetailQuery, overview, project_detail, required_detail
from civ_agent.proposal_gateway import ProposalGateway
from civ_agent.service_io import ServiceIO
from civ_agent.state_updates import changed_details
from civ_agent.tool_schemas import PlayerInspection
from tests.domain_fixtures import Host
from tests.test_selective_actions import options_snapshot, query


def test_controls_timers_statuses_and_promotion_defaults_remain_accessible():
    snapshot = options_snapshot()
    snapshot["turnControls"] = {
        "canDeferChoices": True,
        "canMoveAutomatedUnits": True,
        "autoplayScopes": ["ALL", "MILITARY", "CIVILIANS", "ECONOMY"],
    }
    unit = snapshot["map"]["units"][0]
    unit["statuses"] = [{"name": "Test status", "turnsRemaining": 3, "description": "+1 Movement"}]
    unit["controls"] = {
        "rename": {"available": True},
        "savePromotions": {"available": True},
        "promotionCity": "city1",
        "canConnectRoad": True,
        "swaps": [
            {
                "destination": {"x": 1, "y": 0},
                "partners": ["unit2"],
                "captureRisks": [{"unit": "unit1", "risk": {"threats": []}}],
            }
        ],
    }
    city = snapshot["cities"][0]
    city.setdefault("development", {}).update(
        resistanceTurns=2,
        beingRazed=True,
        demandedResource="Gold",
        weLoveTheKingTurns=4,
        borderExpansion={"cultureStored": 5, "cultureCost": 15, "culturePerTurn": 2, "turns": 5},
    )
    city.setdefault("management", {})["defaultPromotions"] = [
        {"unitType": "Warrior", "enabled": True, "promotions": ["Shock I"], "action": {"available": True}}
    ]
    shown = overview(snapshot)
    assert shown["cities"][0]["borderExpansion"]["turns"] == 5
    assert shown["cities"][0]["resistanceTurns"] == 2
    assert shown["units"][0]["statuses"][0]["turnsRemaining"] == 3
    controls = shown["turnControls"]
    for command in controls["autoplayCommands"] + [controls["deferChoicesCommand"], controls["moveAutomatedCommand"]]:
        assert command_arguments(command) == command
    movement = project_detail(snapshot, query("unit_details", unit["handle"], ["movement"]))["data"]["entity"]
    swap = movement["controls"]["swaps"][0]
    assert swap["command"]["kind"] == "SWAP_UNITS"
    assert "confirmAction" not in swap["command"]  # Exposure is never accepted for the model.
    assert swap["confirmationField"] == "confirmAction"
    production = project_detail(snapshot, query("city_details", city["handle"], ["production"]))["data"]["entity"]
    default = production["management"]["defaultPromotions"][0]["command"]
    assert default == {
        "kind": "SET_DEFAULT_PROMOTIONS",
        "target": city["handle"],
        "selection": "Warrior",
        "enabled": False,
    }
    assert command_arguments({"kind": "RENAME_UNIT", "target": unit["handle"], "selection": ""})["selection"] == ""


@pytest.mark.parametrize(
    "arguments",
    [
        {"subject": "ROAD", "destination": None},
        {"subject": "REPLAY", "turn": None},
        {"subject": "CIVILOPEDIA", "limit": 101},
        {"subject": "CIVILOPEDIA", "names": [""]},
        {"subject": "CIVILOPEDIA", "names": ["Worker"] * 17},
    ],
)
def test_inspection_input_rejects_nulls_and_unbounded_pages(arguments):
    with pytest.raises(ValidationError):
        PlayerInspection.model_validate(arguments)


def test_native_road_read_satisfies_command_and_expires_after_action(tmp_path):
    async def run():
        io = ServiceIO()
        memory = CivilizationState(tmp_path / "private", "Rome")
        gateway = ProposalGateway(memory, io, tmp_path / "workspace")
        host = Host()
        original = host.tool

        async def tool(name, args):
            if name == "player_inspect":
                return {
                    "epoch": "g1",
                    "revision": host.revision,
                    "turn": 0,
                    "subject": "ROAD",
                    "data": {"available": True, "workerTurns": 6},
                }
            return await original(name, args)

        host.tool = tool
        job = await gateway.open("planning", "gameplay", "g1", host, {})
        try:
            await gateway.call(job, "player_turn", {}, "start")
            args = {"subject": "ROAD", "target": "unit1", "destination": {"x": 0, "y": 3}}
            result = await gateway.call(job, "player_inspect", args, "road")
            command = result["data"]["command"]
            assert command["kind"] == "CONNECT_ROAD"
            assert required_detail(command) in job.details_read
            before = {"revision": host.revision}
            receipt = await gateway.call(job, "player_command", command, "connect")
            assert "error" not in receipt
            assert host.executed[-1]["kind"] == "CONNECT_ROAD"
            assert changed_details(
                before, {"revision": host.revision}, [DetailQuery(tool="player_inspect", arguments=args)]
            )
        finally:
            await gateway.close(job, True)
            await io.close()

    asyncio.run(run())


def test_road_unavailable_does_not_offer_a_command():
    result = {"data": {"available": False}}
    add_decision_commands("player_inspect", result, {"subject": "ROAD"})
    assert not result["available"] and "command" not in result["data"]
