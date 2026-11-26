"""Civilian observations reach the model before travel and refresh with ordinary actions."""

import asyncio
import copy
import json

from civ_agent.civilization_state import CivilizationState
from civ_agent.game_commands import add_combat_commands
from civ_agent.observations import DetailQuery, overview, project_detail
from civ_agent.proposal_gateway import ProposalGateway
from civ_agent.protocol import JsonObject
from civ_agent.service_io import ServiceIO
from civ_agent.startup_context import prepare_startup, startup_prompt
from civ_agent.state_updates import apply_overview_update
from civ_agent.tactical_review import tactical_review
from tests.domain_fixtures import Host
from tests.test_tactical_review import france


def settler_position():
    snapshot = france()
    template = copy.deepcopy(snapshot["map"]["units"][0])
    settler = {
        **template,
        "name": "Settler",
        "handle": "settler",
        "position": {"x": -4, "y": 4},
        "military": False,
        "nearbyHostiles": [],
        "protection": {
            "current": {
                "protection": "UNESCORTED",
                "military": None,
                "captureAssessment": "NO_VISIBLE_CAPTURE_THREAT",
                "visibleCaptureThreats": [],
                "unseenTilesWithinTwoHexes": 5,
                "nearbyKnownCamps": [{"x": -4, "y": 7}],
                "nearbyBarbarianSightings": [{"x": -4, "y": 5}],
            },
            "escortCandidates": [
                {
                    "unit": "warrior",
                    "distanceInHexes": 4,
                    "atSettler": False,
                    "canReachThisTurn": False,
                    "estimatedTurns": None,
                    "routeStatus": "NO_MOVEMENT_REMAINING",
                }
            ],
            "nextStop": None,
        },
    }
    warrior: JsonObject = {**copy.deepcopy(template), "handle": "warrior", "name": "Warrior", "military": True}
    warrior["combat"]["order"]["action"] = "Explore"
    snapshot["map"]["units"] = [settler, warrior]
    snapshot["map"]["tiles"] = [
        {
            "position": {"x": -4, "y": 7},
            "visible": False,
            "observedTurn": 57,
            "improvement": "Barbarian encampment",
            "units": [],
        },
        {
            "position": {"x": 6, "y": 0},
            "visible": True,
            "observedTurn": 60,
            "improvement": "Barbarian encampment",
            "units": [],
        },
    ]
    snapshot["map"]["recentBarbarianSightings"] = [{"position": {"x": -4, "y": 5}, "name": "Brute", "lastSeenTurn": 58}]
    snapshot["turn"] = 60
    add_combat_commands(snapshot)
    return snapshot


def test_camps_sightings_and_unescorted_settlers_are_present_without_visible_hostiles():
    snapshot = settler_position()
    snapshot["tacticalReview"] = tactical_review(snapshot).model_dump(exclude_none=True)
    before = copy.deepcopy(snapshot)
    view = overview(snapshot)
    tactics = view["tacticalReview"]
    assert tactics["visibleHostiles"] == []
    assert tactics["knownBarbarianCamps"] == [
        {"position": {"x": -4, "y": 7}, "visible": False, "lastObservedTurn": 57},
        {"position": {"x": 6, "y": 0}, "visible": True, "lastObservedTurn": 60},
    ]
    assert tactics["recentBarbarianSightings"][0]["turnsAgo"] == 2
    assert view["units"][1]["order"] == "Explore"
    protection = view["units"][0]["protection"]
    assert protection["current"]["captureAssessment"] == "NO_VISIBLE_CAPTURE_THREAT"
    assert protection["current"]["unseenTilesWithinTwoHexes"] == 5
    candidate = protection["escortCandidates"][0]
    assert candidate["rendezvousPreview"] == {
        "tool": "movement_preview",
        "arguments": {"target": "warrior", "destination": {"x": -4, "y": 4}},
    }
    assert candidate["ordersInspection"]["arguments"] == {"target": "warrior", "sections": ["orders"]}
    assert snapshot == before, "Command templates mutated the saved native observation"
    for section in ("movement", "settlement", "orders"):
        result = project_detail(
            snapshot, DetailQuery(tool="unit_details", arguments={"target": "settler", "sections": [section]})
        )
        assert result["data"]["entity"]["protection"] == protection


def test_same_tile_candidate_offers_pairing_inspection_instead_of_a_redundant_move():
    snapshot = settler_position()
    candidate = snapshot["map"]["units"][0]["protection"]["escortCandidates"][0]
    candidate.update(atSettler=True, canReachThisTurn=True, estimatedTurns=0, routeStatus="AT_SETTLER")
    shown = overview(snapshot)["units"][0]["protection"]["escortCandidates"][0]
    assert shown["pairingInspection"] == {
        "tool": "unit_details",
        "arguments": {"target": "settler", "sections": ["orders"]},
    }
    assert "rendezvousPreview" not in shown
    assert "command" not in shown  # Actual pairing availability comes from native orders.


def test_startup_and_command_deltas_include_new_settlers_and_clear_expired_sightings(tmp_path):
    async def run():
        io = ServiceIO()
        memory = CivilizationState(tmp_path / "private", "France")
        gateway = ProposalGateway(memory, io, tmp_path / "workspace")
        host = Host()
        host.civilization = "France"
        host.view = settler_position()
        original = host.tool

        async def tool(name, args):
            if name in {"player_capabilities", "victory_progress"}:
                return {}
            return await original(name, args)

        host.tool = tool
        try:
            job = await gateway.open("protect", "gameplay", "g1", host, {}, game_turn=60)
            prepared = await prepare_startup(gateway, job)
            prompt = startup_prompt(prepared, "gameplay", "Play this turn")
            reads = json.JSONDecoder().raw_decode(prompt.user.split("Current game information:\n", 1)[1])[0]
            initial = reads["player_turn"]
            assert initial["units"][0]["protection"]["current"]["protection"] == "UNESCORTED"
            assert initial["tacticalReview"]["recentBarbarianSightings"][0]["lastSeenTurn"] == 58
            city = await gateway.call(job, "city_details", {"target": "city2", "sections": ["combat"]}, "inspect")
            shot = city["data"]["entity"]["attacks"][0]["command"]
            fresh = copy.deepcopy(host.view["map"]["units"][0])
            fresh["handle"] = "new-settler"
            host.view["map"]["units"].append(fresh)
            host.view["map"]["units"][0]["protection"]["current"]["visibleCaptureThreats"] = [{"x": -4, "y": 5}]
            host.view["map"]["units"][0]["protection"]["current"]["captureAssessment"] = "VISIBLE_CAPTURE_THREAT"
            host.view["map"]["recentBarbarianSightings"] = []
            for unit in host.view["map"]["units"]:
                if unit.get("protection"):
                    unit["protection"]["current"]["nearbyBarbarianSightings"] = []
                    unit["protection"]["current"]["nearbyKnownCamps"] = []
            host.view["map"]["tiles"].pop(0)
            result = await gateway.call(job, "player_command", {**shot, "intent": {"reason": "Defend the city"}}, "act")
            current = apply_overview_update(initial, result["stateUpdate"])
            assert [u["handle"] for u in current["units"]] == ["settler", "warrior", "new-settler"]
            assert current["units"][0]["protection"]["current"]["captureAssessment"] == "VISIBLE_CAPTURE_THREAT"
            assert current["units"][2]["protection"]["current"]["protection"] == "UNESCORTED"
            assert current["tacticalReview"]["recentBarbarianSightings"] == []
            assert len(current["tacticalReview"]["knownBarbarianCamps"]) == 1
            original_units = {unit["handle"]: unit for unit in initial["units"]}
            assert [unit["handle"] for unit in current["units"] if unit != original_units.get(unit["handle"])] == [
                "settler",
                "new-settler",
            ]
            assert '"handle": "warrior"' not in json.dumps(result["stateUpdate"])
        finally:
            await io.close()

    asyncio.run(run())
