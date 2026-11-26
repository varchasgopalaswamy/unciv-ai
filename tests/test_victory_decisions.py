import asyncio

import pytest
from jsonschema import Draft202012Validator
from pydantic import ValidationError

from civ_agent.civilization_state import CivilizationState
from civ_agent.game_commands import command_arguments, command_schema
from civ_agent.observations import overview, required_detail
from civ_agent.proposal_gateway import ProposalGateway
from civ_agent.protocol import JsonObject
from civ_agent.result_schema import DiplomaticBallot
from civ_agent.role_view import role_overview
from civ_agent.service_io import ServiceIO
from tests.domain_fixtures import Host
from tests.test_tactical_review import reviewed


def results() -> JsonObject:
    return {
        "minimumVotes": 3,
        "winner": "Greece",
        "unmetWinner": False,
        "rows": [
            {
                "civilizationId": "Rome",
                "name": "Rome",
                "votes": 2,
                "votesReceived": 0,
                "choice": "VOTED",
                "votedFor": "Greece",
            }
        ],
        "canAcknowledge": False,
    }


@pytest.mark.parametrize(
    "command",
    [
        {"kind": "ADD_SPACESHIP_PART", "target": "unit1"},
        {"kind": "CAST_DIPLOMATIC_VOTE", "selection": "Greece"},
        {"kind": "ABSTAIN_DIPLOMATIC_VOTE"},
        {"kind": "ACKNOWLEDGE_DIPLOMATIC_RESULTS"},
    ],
)
def test_victory_commands_have_explicit_shapes_and_reads(command):
    validator = Draft202012Validator(command_schema())
    assert validator.is_valid(command)
    assert command_arguments(command) == command
    assert required_detail(command) is None
    invalid = {**command, "destination": {"x": 0, "y": 0}}
    with pytest.raises(ValidationError):
        command_arguments(invalid)
    if command["kind"] == "CAST_DIPLOMATIC_VOTE":
        for selection in (None, "", True):
            with pytest.raises(ValidationError):
                command_arguments({**command, "selection": selection})


def test_overview_preserves_whole_election_and_discovers_assembly():
    view = reviewed()
    view["victory"] = {
        "ballot": {
            "turnsUntilNextVote": 0,
            "hasVoted": False,
            "votedFor": None,
            "canVote": True,
            "candidates": [{"civilizationId": "Greece", "name": "Greece"}],
            "cityStateVoteTiming": {
                "window": "CLOSED",
                "lastInfluenceTurn": None,
                "guidance": "Your opportunity to influence the scheduled votes has passed.",
            },
        },
        "votingResults": results(),
    }
    view["map"]["units"][0]["spaceshipAssembly"] = {"available": True, "consumesUnit": True}
    shown = overview(view)
    assert shown["victory"] == view["victory"]
    assert role_overview(shown, "main")["victory"] == view["victory"]
    ballot = view["victory"]["ballot"]
    assert DiplomaticBallot.model_validate(ballot).model_dump() == ballot
    older_ballot = {key: value for key, value in ballot.items() if key != "cityStateVoteTiming"}
    assert DiplomaticBallot.model_validate(older_ballot).cityStateVoteTiming is None
    assert shown["units"][0]["canAssembleSpaceshipPart"]


def test_gateway_assembles_without_part_details_and_forwards_vote(tmp_path):
    async def run():
        io = ServiceIO()
        state = CivilizationState(tmp_path / "state", "Rome")
        gateway = ProposalGateway(state, io, tmp_path / "workspace")
        host = Host()
        host.view["victory"] = {"votingResults": results()}
        host.view["map"]["units"][0]["spaceshipAssembly"] = {"available": True, "consumesUnit": True}
        job = await gateway.open("game", "gameplay", "g1", host, {})
        try:
            shown = await gateway.call(job, "player_turn", {}, "read")
            assert shown["victory"]["votingResults"] == results()
            command = {"kind": "ADD_SPACESHIP_PART", "target": "unit1", "intent": {"reason": "Complete the ship"}}
            response = await gateway.call(job, "player_command", command, "unread-part")
            assert response["done"] and len(host.executed) == 1
            await gateway.call(job, "unit_details", {"target": "unit1"}, "part")
            assert await gateway.call(job, "player_command", command, "unread-part") == response
            assert host.executed[-1]["kind"] == "ADD_SPACESHIP_PART"
            await gateway.call(job, "player_turn", {}, "refresh")
            await gateway.call(
                job,
                "player_command",
                {"kind": "CAST_DIPLOMATIC_VOTE", "selection": "Greece", "intent": {"reason": "Support Greece"}},
                "vote",
            )
            assert host.executed[-1]["selection"] == "Greece"
            assert host.executed[-1]["revision"] == "rev2"
        finally:
            await io.close()

    asyncio.run(run())
