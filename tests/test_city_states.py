import asyncio
import copy

import pytest
from jsonschema import Draft202012Validator
from pydantic import ValidationError

from civ_agent.civilization_state import CivilizationState
from civ_agent.game_commands import command_arguments, command_schema
from civ_agent.memory_records import REQUIRED_COMMANDS
from civ_agent.observations import overview, required_detail
from civ_agent.proposal_gateway import ProposalGateway
from civ_agent.service_io import ServiceIO
from tests.test_proposal_gateway import Host
from tests.test_tactical_review import reviewed


def city_state():
    return {
        "civilizationId": "Geneva",
        "name": "Geneva",
        "type": "Cultured",
        "atWar": False,
        "relationship": "Friend",
        "influence": 40,
        "decayPerTurn": 1,
        "ally": None,
        "protecting": False,
        "quests": [{"name": "Give Gold", "influenceReward": 40}],
        "actions": [{"name": "giftGold250", "available": True, "goldChange": -250}],
    }


@pytest.mark.parametrize(
    "command,tool",
    [
        ({"kind": "CITY_STATE_ACTION", "target": "Geneva", "selection": "giftGold250"}, "player_details"),
        (
            {
                "kind": "CITY_STATE_ACTION",
                "target": "Geneva",
                "selection": "pledgeProtection",
                "confirmAction": True,
            },
            "player_details",
        ),
        ({"kind": "GIFT_UNIT", "target": "unit1"}, "unit_details"),
    ],
)
def test_city_state_command_shapes_and_read_requirements(command, tool):
    schema = Draft202012Validator(command_schema())
    assert schema.is_valid(command)
    assert command_arguments(command) == command
    detail = required_detail(command)
    assert detail is not None and detail.tool == tool
    assert command["kind"] in REQUIRED_COMMANDS
    for extra in ({"destination": {"x": 0, "y": 0}}, {"amount": 300}, {"confirmWar": True}):
        invalid = {**command, **extra}
        assert not schema.is_valid(invalid)
        with pytest.raises(ValidationError):
            command_arguments(invalid)


def test_city_states_and_gifts_are_discoverable_in_compact_observations():
    view = reviewed()
    view.setdefault("diplomacy", {})["cityStates"] = [city_state()]
    view["map"]["units"][0]["cityStateGift"] = {"civilizationId": "Geneva", "action": {"available": True}}
    compact = overview(view)
    assert compact["cityStates"][0]["influence"] == 40
    assert compact["cityStates"][0]["quests"] == view["diplomacy"]["cityStates"][0]["quests"]
    assert "actions" not in compact["cityStates"][0]
    assert compact["units"][0]["canGiftToCityState"]


def test_gateway_reads_native_choices_and_keeps_city_state_evidence(tmp_path):
    async def run():
        io = ServiceIO()
        state = CivilizationState(tmp_path / "state", "Rome")
        gateway = ProposalGateway(state, io, tmp_path / "workspace")
        host = Host()
        host.view.setdefault("diplomacy", {})["cityStates"] = [city_state()]
        job = await gateway.open("game", "gameplay", "g1", host, {})
        command = {
            "kind": "CITY_STATE_ACTION",
            "target": "Geneva",
            "selection": "giftGold250",
            "intent": {"reason": "Complete Geneva's quest"},
        }
        try:
            await gateway.call(job, "player_turn", {}, "read")
            response = await gateway.call(job, "player_command", command, "unread")
            assert response["error"] == "DETAIL_REQUIRED"
            assert not host.executed
            detail = await gateway.call(job, "player_details", {"section": "diplomacy"}, "choices")
            assert detail["data"]["diplomacy"]["cityStates"][0]["actions"][0]["name"] == "giftGold250"
            await gateway.call(job, "player_command", command, "gift")
            assert host.executed[-1]["kind"] == "CITY_STATE_ACTION"
            assert host.executed[-1]["selection"] == "giftGold250"
            before = copy.deepcopy(job.memory["records"]["observation:diplomacy/Geneva"])
            host.view["diplomacy"]["cityStates"][0]["influence"] = 75
            await gateway.call(job, "player_turn", {}, "refresh")
            after = job.memory["records"]["observation:diplomacy/Geneva"]
            assert after["version"] > before["version"]
            assert after["body"]["relation"]["influence"] == 75
        finally:
            await io.close()

    asyncio.run(run())
