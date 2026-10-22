"""Civilization mechanics reach gameplay and remain compact in action updates."""

import asyncio
import copy
import json

from civ_agent.civilization_state import CivilizationState
from civ_agent.model_view import model_view
from civ_agent.observations import civilization_profile_summary, overview
from civ_agent.proposal_gateway import ProposalGateway
from civ_agent.service_io import ServiceIO
from civ_agent.startup_context import prepare_startup, startup_prompt
from civ_agent.state_updates import apply_overview_update, overview_update, stamp
from tests.test_strategic_review import host_at


def french_profile():
    return {
        "name": "France",
        "ability": {
            "name": "Ancien Régime",
            "effects": [
                {
                    "text": "+2 Culture in all cities before discovering Steam Power",
                    "technologyConditions": [{"condition": "before discovering Steam Power", "met": True}],
                }
            ],
        },
        "uniqueUnits": [
            {
                "name": "Musketeer",
                "replaces": "Musketman",
                "unitType": "Gunpowder",
                "baseStats": {"productionCost": 120, "strength": 20, "movement": 2},
                "effects": [],
                "freePromotions": [],
                "requiredTechnologies": ["Gunpowder"],
                "missingTechnologies": ["Gunpowder"],
                "resourceRequirements": {},
                "requiredBuilding": None,
                "requiredNearbyImprovedResources": [],
                "obsoletedBy": ["Rifling"],
                "obsolete": False,
                "upgradesTo": ["Rifleman"],
                "comparison": {
                    "baseStats": {"strength": {"ordinary": 16, "unique": 20}},
                    "resourceRequirements": {},
                    "addedEffects": [],
                    "removedEffects": [],
                    "addedPromotions": [],
                    "removedPromotions": [],
                },
            }
        ],
        "uniqueBuildings": [],
        "comparisonBasis": "Base ruleset stats and inherent effects.",
    }


def test_compaction_preserves_conditions_and_replacement_losses_without_mutating_the_snapshot():
    snapshot = copy.deepcopy(host_at(0).view)
    snapshot.update(phase="ACTIVE", civilizationProfile=french_profile())
    snapshot["civilizationProfile"]["ability"]["effects"][0]["technologyConditions"][0]["met"] = False
    before = copy.deepcopy(snapshot)
    profile = model_view(overview(snapshot))["civilizationProfile"]
    assert profile["ability"] == snapshot["civilizationProfile"]["ability"]
    assert profile["ability"]["effects"][0]["technologyConditions"][0]["met"] is False
    unit = profile["uniqueUnits"][0]
    assert unit["obsolete"] is False and unit["missingTechnologies"] == ["Gunpowder"]
    assert "uniqueBuildings" not in profile and "requiredBuilding" not in unit and "freePromotions" not in unit
    assert unit["comparison"] == {"baseStats": {"strength": {"ordinary": 16, "unique": 20}}}
    profile["ability"]["effects"][0]["text"] = "edited local copy"
    assert snapshot == before

    # Removing a cost, prerequisite or promotion is meaningful, even when its new value is empty.
    differences = {
        "baseStats": {"maintenance": {"ordinary": 2, "unique": 0}},
        "requiredTechnologies": {"ordinary": ["Archery"], "unique": []},
        "requiredBuilding": {"ordinary": "Monument", "unique": None},
    }
    assert civilization_profile_summary({"comparison": differences}) == {"comparison": differences}


def test_technology_changes_refresh_the_profile_but_ordinary_actions_do_not_repeat_it():
    snapshot = copy.deepcopy(host_at(0).view)
    snapshot.update(phase="ACTIVE", civilizationProfile=french_profile())
    initial = stamp(overview(snapshot), "turn", "g1", "1")
    snapshot["map"]["units"][0]["movement"] = "1"
    moved = stamp(overview(snapshot), "turn", "g1", "2")
    delta = overview_update(initial, moved, [])
    assert "civilizationProfile" not in delta["set"]
    assert apply_overview_update(initial, delta) == moved
    profile = snapshot["civilizationProfile"]
    profile["ability"]["effects"][0]["technologyConditions"][0]["met"] = False
    profile["uniqueUnits"][0].update(missingTechnologies=[], obsolete=True)
    researched = stamp(overview(snapshot), "turn", "g1", "3")
    assert researched["civilizationProfile"]["uniqueUnits"][0]["missingTechnologies"] == []
    delta = overview_update(moved, researched, [])
    assert delta["set"]["civilizationProfile"] == researched["civilizationProfile"]
    assert apply_overview_update(moved, delta) == researched
    assert initial["civilizationProfile"]["uniqueUnits"][0]["obsolete"] is False


def test_first_turn_preloads_future_uniques_through_the_existing_reads(tmp_path):
    async def run():
        io = ServiceIO()
        gateway = ProposalGateway(CivilizationState(tmp_path / "private", "France"), io, tmp_path / "workspace")
        host = host_at(0)
        host.civilization = "France"
        host.view.update(civilizationProfile=french_profile(), cities=[])
        calls = []
        original = host.tool

        async def tool(name, args):
            calls.append(name)
            return await original(name, args)

        host.tool = tool
        try:
            job = await gateway.open("opening", "gameplay", "g1", host, {})
            prepared = await prepare_startup(gateway, job)
            prompt = startup_prompt(prepared, "gameplay", "Play this turn")
            reads = json.JSONDecoder().raw_decode(prompt.user.split("Current game information:\n", 1)[1])[0]
            profile = reads["player_turn"]["civilizationProfile"]
            assert reads["player_turn"]["cities"] == []
            assert profile["ability"]["effects"][0]["technologyConditions"][0]["met"] is True
            assert profile["uniqueUnits"][0]["missingTechnologies"] == ["Gunpowder"]
            assert profile["uniqueUnits"][0]["comparison"]["baseStats"]["strength"] == {"ordinary": 16, "unique": 20}
            assert calls.count("player_turn") == 1
            assert set(calls) == {"player_turn", "player_capabilities", "victory_progress", "player_communication"}
        finally:
            await io.close()

    asyncio.run(run())
