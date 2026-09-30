"""Promotion tradeoffs stay visible without preloading the entire promotion tree."""

import asyncio
import copy
import json

from civ_agent.civilization_state import CivilizationState
from civ_agent.model_view import model_view
from civ_agent.observations import DetailQuery, overview, project_detail
from civ_agent.proposal_gateway import ProposalGateway
from civ_agent.service_io import ServiceIO
from civ_agent.startup_context import prepare_startup, startup_prompt
from civ_agent.state_updates import apply_overview_update, overview_update, stamp
from tests.test_proposal_gateway import Host
from tests.test_selective_actions import options_snapshot


def promotion_position():
    snapshot = options_snapshot()
    unit = snapshot["map"]["units"][0]
    unit.update(name="Warrior", health=40, military=True)
    common = {"acquired": False, "available": True, "unavailableReasons": [], "prerequisites": [], "experienceCost": 10}
    after = {"experienceRemaining": 3, "nextPromotionCost": 20, "experienceNeededForNextPromotion": 17}
    unit["combat"]["promotions"] = {
        "experience": 13,
        "experienceForNextPromotion": 10,
        "earnedPromotions": 0,
        "totalExperience": 13,
        "barbarianExperienceLimit": 30,
        "canEarnExperienceFromBarbarians": True,
        "recovery": {
            "healingThisTurn": 25,
            "healingPerRestTurn": 25,
            "turnEndsToFullHealth": 3,
            "basis": "Rest without a promotion on the current tile with unchanged healing bonuses and no further damage; count turn ends starting with this turn.",
        },
        "choices": [
            {
                **common,
                "name": "Heal Instantly",
                "description": "This Unit heals 50 HP\nDoing so will consume this opportunity to choose a Promotion",
                "retainedAfterSelection": False,
                "prerequisiteUnlocks": [],
                "afterSelection": after,
            },
            {
                **common,
                "name": "Drill I",
                "description": "+15% Strength when fighting in Rough terrain tiles",
                "retainedAfterSelection": True,
                "prerequisiteUnlocks": [
                    {"name": "Drill II", "description": "+15% Strength in Rough terrain; requires Drill I"}
                ],
                "afterSelection": after,
            },
            {
                **common,
                "name": "Drill II",
                "description": "+15% Strength in Rough terrain; requires Drill I",
                "available": False,
                "prerequisites": ["Drill I"],
                "unavailableReasons": ["Requires one of: Drill I"],
                "retainedAfterSelection": True,
                "prerequisiteUnlocks": [],
            },
        ],
    }
    return snapshot


def test_overview_compares_legal_choices_and_keeps_the_full_tree_in_details():
    snapshot = promotion_position()
    before = copy.deepcopy(snapshot)
    unit = model_view(overview(snapshot))["units"][0]
    shown = unit["promotions"]
    assert "availablePromotions" not in unit
    assert shown["acquired"] == []
    assert shown["recovery"]["turnEndsToFullHealth"] == 3
    assert "no further damage" in shown["recovery"]["basis"]
    heal, drill = shown["choices"]
    assert heal["name"] == "Heal Instantly" and heal["retainedAfterSelection"] is False
    assert heal["afterSelection"]["experienceNeededForNextPromotion"] == 17
    assert heal["description"].endswith("consume this opportunity to choose a Promotion")
    assert drill["retainedAfterSelection"] is True
    assert drill["prerequisiteUnlocks"][0]["name"] == "Drill II"
    assert drill["command"] == {"kind": "PROMOTE_UNIT", "target": unit["handle"], "selection": "Drill I"}
    inspection = unit["inspections"]["PROMOTE_UNIT"]
    assert inspection["arguments"]["sections"] == ["promotions"]
    detail = model_view(project_detail(snapshot, DetailQuery.model_validate(inspection)))["data"]["entity"]
    choices = detail["combat"]["promotions"]["choices"]
    assert choices[0]["afterSelection"] == heal["afterSelection"]
    assert choices[1]["command"] == drill["command"]
    assert len(choices) == 3 and "command" not in choices[2]
    assert snapshot == before, "Overview or selective detail changed the stored snapshot"


def test_consumed_choices_disappear_from_deltas_but_experience_limit_and_acquired_promotions_remain():
    snapshot = promotion_position()
    before = stamp(overview(snapshot), "turn", "epoch", "1")
    status = snapshot["map"]["units"][0]["combat"]["promotions"]
    status.update(
        experience=1,
        experienceForNextPromotion=30,
        earnedPromotions=2,
        totalExperience=31,
        canEarnExperienceFromBarbarians=False,
    )
    for choice in status["choices"]:
        choice.update(available=False, unavailableReasons=["Not enough experience"])
        choice.pop("afterSelection", None)
    status["choices"][1]["acquired"] = True
    after = stamp(overview(snapshot), "turn", "epoch", "2")
    delta = overview_update(before, after, [])
    assert apply_overview_update(before, delta) == after
    shown = after["units"][0]["promotions"]
    assert shown["acquired"] == ["Drill I"]
    assert shown["experience"] == 1 and shown["experienceForNextPromotion"] == 30
    assert shown["totalExperience"] == 31 and shown["canEarnExperienceFromBarbarians"] is False
    assert "choices" not in shown and "recovery" not in shown


def test_units_without_experience_or_promotions_do_not_add_empty_comparisons():
    snapshot = promotion_position()
    status = snapshot["map"]["units"][0]["combat"]["promotions"]
    status.update(experience=0, totalExperience=0)
    for choice in status["choices"]:
        choice.update(available=False)
    unit = overview(snapshot)["units"][0]
    assert "promotions" not in unit
    assert "PROMOTE_UNIT" not in unit["inspections"]


def test_preloaded_comparison_leads_to_an_executable_promotion_with_its_focused_inspection(tmp_path):
    async def run():
        io = ServiceIO()
        memory = CivilizationState(tmp_path / "private", "Rome")
        gateway = ProposalGateway(memory, io, tmp_path / "workspace")
        host = Host()
        host.view = promotion_position()
        original = host.tool

        async def tool(name, args):
            if name in {"player_capabilities", "victory_progress"}:
                return {}
            return await original(name, args)

        host.tool = tool
        try:
            job = await gateway.open("promote", "gameplay", "g1", host, {})
            prepared = await prepare_startup(gateway, job)
            prompt = startup_prompt(prepared, "gameplay", "Play this turn")
            reads = json.JSONDecoder().raw_decode(prompt.user.split("Prepared player-visible reads:\n", 1)[1])[0]
            unit = reads["player_turn"]["units"][0]
            assert unit["promotions"]["recovery"]["turnEndsToFullHealth"] == 3
            choice = unit["promotions"]["choices"][1]
            assert choice["name"] == "Drill I" and choice["retainedAfterSelection"] is True
            inspection = unit["inspections"]["PROMOTE_UNIT"]
            await gateway.call(job, inspection["tool"], inspection["arguments"], "inspect")
            result = await gateway.call(
                job,
                "player_command",
                {**choice["command"], "intent": {"reason": "Improve future combat in rough terrain"}},
                "promote",
            )
            assert "error" not in result
            assert host.executed[-1]["kind"] == "PROMOTE_UNIT" and host.executed[-1]["selection"] == "Drill I"
        finally:
            await io.close()

    asyncio.run(run())
