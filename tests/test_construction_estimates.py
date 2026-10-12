import asyncio
import copy
import json
from types import SimpleNamespace
from typing import Literal

from civ_agent.civilization_state import CivilizationState
from civ_agent.construction_estimates import investment_comparison
from civ_agent.observations import DetailQuery, overview, project_detail
from civ_agent.proposal_gateway import ProposalGateway
from civ_agent.protocol import JsonObject
from civ_agent.service_io import ServiceIO
from civ_agent.startup_context import prepare_startup
from civ_agent.state_updates import changed_details, overview_update, stamp
from tests.test_agent_service import StartupHost


def estimate(science="3", *, turns=5, slots=None):
    return {
        "production": {"cost": 60, "stored": 10, "overflow": 0, "perTurn": 10, "turnsIfStartedNow": turns},
        "queueUnavailableReasons": [],
        "whileBuilding": {
            "before": {"Science": "11", "Food": "2"},
            "after": {"Science": "10", "Food": "2"},
            "change": {"Science": "-1"},
        },
        "completion": {
            "yields": {
                "before": {"Science": "10", "Gold": "3"},
                "after": {"Science": str(10 + int(science)), "Gold": "2"},
                "change": {"Science": science, "Gold": "-1"},
            },
            "maintenanceChange": "1",
            "sourceChanges": {"Buildings": {"Science": science}, "Maintenance": {"Gold": "-1"}},
            "specialistSlots": slots or {},
            "greatPersonPoints": {},
        },
    }


def snapshot():
    host = StartupHost(
        SimpleNamespace(service=SimpleNamespace(active_id="job", records={"job": {"epoch": "g1", "gameTurn": 0}}))
    )
    city = host.view["cities"][0]
    host.view["constructionCatalogue"] = ["Library", "University", "The Great Library", "Apollo Program", "SS Booster"]
    host.view["phase"] = "ACTIVE"
    city.update(
        constructionQueue=["Library"],
        constructionOptions=["Library", "University", "The Great Library"],
        constructionChoice={"availableSinceChoice": ["University"]},
        constructionEstimates={
            "Library": estimate(),
            "University": estimate("6", turns=9, slots={"Scientist": 2}),
            "The Great Library": estimate("3", turns=12),
        },
    )
    host.view["wonderOpportunities"] = [
        {
            "name": "The Great Library",
            "kind": "WORLD",
            "benefits": "Free Technology",
            "cities": [{"city": "city1", "canQueue": True, "queued": False, "productionCost": 140, "turns": 12}],
        }
    ]
    return host


def query(names=None, *, tool: Literal["construction_details", "city_details"] = "construction_details", sections=None):
    args = {"target": "city1"}
    if names is not None:
        args["names"] = names
    if sections is not None:
        args["sections"] = sections
    return DetailQuery(tool=tool, arguments=args)


def test_current_and_new_choices_share_compact_estimates_with_wonders():
    view = snapshot().view
    view["cities"][0]["constructionChoice"]["availableSinceChoice"].append("The Great Library")
    view["wonderOpportunities"][0]["cities"][0].update(productionStored=20, productionPerTurn=10)
    raw = copy.deepcopy(view)
    compact = overview(view)
    options = compact["cities"][0]["constructionEstimates"]
    assert set(options) == {"Library", "University"}
    assert options["Library"]["completion"]["yieldChange"] == {"Science": "3", "Gold": "-1"}
    assert options["Library"]["whileBuildingChange"] == {"Science": "-1"}
    assert options["University"]["completion"]["specialistSlots"] == {"Scientist": 2}
    assert compact["wonderOpportunities"][0]["cities"][0]["completion"] == options["Library"]["completion"]
    wonder = compact["wonderOpportunities"][0]["cities"][0]
    assert wonder["productionStored"] == 20 and wonder["productionPerTurn"] == 10
    assert wonder["whileBuildingChange"] == {"Science": "-1"}
    view["cities"][0]["constructionQueue"] = ["The Great Library"]
    assert "The Great Library" not in overview(view)["cities"][0]["constructionEstimates"]
    view["cities"][0]["constructionQueue"] = raw["cities"][0]["constructionQueue"]
    assert "sourceChanges" not in json.dumps(compact)
    assert '"before":' not in json.dumps(compact)
    assert "current population" in compact["constructionEstimateBasis"]
    assert view == raw


def test_compact_building_and_wonder_estimates_omit_zero_yields_but_named_reads_preserve_them():
    view = snapshot().view
    for estimate in view["cities"][0]["constructionEstimates"].values():
        estimate["completion"]["yields"]["change"].update(Food="0", Production="-0.0", Culture="0.000", Happiness=0)
    raw = copy.deepcopy(view)
    compact = overview(view)
    building = compact["cities"][0]["constructionEstimates"]["Library"]["completion"]
    wonder = compact["wonderOpportunities"][0]["cities"][0]["completion"]
    assert building["yieldChange"] == wonder["yieldChange"] == {"Science": "3", "Gold": "-1"}
    assert "The Great Library" not in compact["cities"][0]["constructionEstimates"]
    named = project_detail(view, query(["Library", "The Great Library"]))["data"]["constructions"]
    for row in named:
        assert row["completion"]["yields"]["change"]["Food"] == "0"
        assert row["completion"]["yields"]["change"]["Production"] == "-0.0"
    assert view == raw


def test_catalogue_is_compact_and_named_reads_retain_accounting_and_commands():
    view = snapshot().view
    catalogue = project_detail(view, query())["data"]
    assert len(catalogue["constructions"]) == 3
    assert "sourceChanges" not in json.dumps(catalogue)
    selected = project_detail(view, query(["Library", "University"]))["data"]
    library, university = selected["constructions"]
    assert library["completion"]["yields"]["after"]["Science"] == "13"
    assert library["completion"]["sourceChanges"]["Maintenance"] == {"Gold": "-1"}
    assert library["production"]["turnsIfStartedNow"] == 5
    assert university["completion"]["specialistSlots"] == {"Scientist": 2}
    assert library["command"] == {"kind": "QUEUE_CONSTRUCTION", "target": "city1", "selection": "Library"}
    assert "estimateBasis" in selected
    unknown = project_detail(view, query(["Librarry"]))
    assert not unknown["available"] and not unknown["data"]["constructions"]
    assert unknown["data"]["unknownNames"][0] == {
        "name": "Librarry",
        "code": "UNKNOWN_CONSTRUCTION",
        "suggestions": ["Library"],
    }
    assert unknown["data"]["discovery"] == {"tool": "construction_details", "arguments": {"target": "city1"}}
    mixed = project_detail(view, query(["Library", "Librarry"]))
    assert not mixed["available"] and "command" not in mixed["data"]["constructions"][0]


def test_known_locked_construction_and_puppet_are_explained_separately_from_unknown_names():
    view = snapshot().view
    view["constructionCatalogue"] = ["Library", "University", "Apollo Program", "Laboratory"]
    locked = project_detail(view, query(["Laboratory"]))
    assert locked["available"] and "unknownNames" not in locked["data"]
    assert locked["data"]["detailLevel"] == "named" and "descriptionsIncluded" not in locked["data"]
    row = locked["data"]["constructions"][0]
    assert not row["canQueue"] and row["queueUnavailableReasons"]
    city = view["cities"][0]
    city["puppet"] = True
    city["constructionOptions"] = []
    row = project_detail(view, query(["Library"]))["data"]["constructions"][0]
    assert not row["canQueue"] and "command" not in row
    assert "Puppet cities control their own production" in row["queueUnavailableReasons"]
    assert "Laboratory" in project_detail(view, query())["data"]["knownNames"]
    assert "constructionCatalogue" not in overview(view)


def test_queued_wonder_keeps_blockers_and_overflow_in_its_single_overview_entry():
    view = snapshot().view
    city = view["cities"][0]
    city["constructionQueue"] = ["The Great Library"]
    estimate = city["constructionEstimates"]["The Great Library"]
    estimate["queueUnavailableReasons"] = ["Already built elsewhere"]
    estimate["production"]["overflow"] = 12
    view["wonderOpportunities"][0]["cities"][0].update(queued=True, canQueue=False)
    compact = overview(view)
    row = compact["wonderOpportunities"][0]["cities"][0]
    assert row["queued"] and row["queueUnavailableReasons"] == ["Already built elsewhere"]
    assert row["productionOverflow"] == 12
    assert "The Great Library" not in compact["cities"][0]["constructionEstimates"]


def test_native_growth_rounding_is_preserved_even_when_added_food_does_not_accelerate_growth():
    view = snapshot().view
    completion = view["cities"][0]["constructionEstimates"]["Library"]["completion"]
    completion["yields"]["change"]["Food"] = "0.5"
    growth: dict[str, int | None] = {
        "foodPerTurnBefore": 0,
        "foodPerTurnAfter": 1,
        "turnsToGrowthBefore": None,
        "turnsToGrowthAfter": 15,
        "turnsToStarvationBefore": None,
        "turnsToStarvationAfter": None,
    }
    completion["growth"] = growth
    compact = overview(view)["cities"][0]["constructionEstimates"]["Library"]["completion"]
    assert compact["growth"] == growth
    assert compact["yieldChange"]["Food"] == "0.5"
    detailed = project_detail(view, query(["Library"]))["data"]["constructions"][0]["completion"]
    assert detailed["growth"] == growth
    growth["foodPerTurnBefore"] = 1
    growth["turnsToGrowthBefore"] = 15
    unchanged = overview(view)["cities"][0]["constructionEstimates"]["Library"]["completion"]
    assert unchanged["growth"] == growth
    completion["yields"]["change"]["Food"] = "0"
    unchanged = overview(view)["cities"][0]["constructionEstimates"]["Library"]["completion"]
    assert "growth" not in unchanged


def test_city_sections_keep_estimates_with_production_and_actual_yields_with_economy():
    view = snapshot().view
    production = project_detail(view, query(tool="city_details", sections=["production"]))["data"]["entity"]
    assert set(production["constructionEstimates"]) == {"Library"}
    assert "constructionEstimateBasis" in production
    economy = project_detail(view, query(tool="city_details", sections=["economy"]))["data"]["entity"]
    assert "constructionEstimates" not in economy
    complete = project_detail(view, query(tool="city_details"))["data"]["entity"]
    assert len(complete["constructionEstimates"]) == 3 and "constructionEstimateBasis" in complete


def test_changed_benefits_invalidate_selected_reads_and_only_update_affected_rows():
    before = snapshot().view
    after = copy.deepcopy(before)
    after["cities"][0]["constructionEstimates"]["University"] = estimate("8", turns=7, slots={"Scientist": 2})
    library, university = query(["Library"]), query(["University"])
    assert changed_details(before, after, [library, university]) == [university]
    delta = overview_update(
        stamp(overview(before), "job", "g1", "r1"), stamp(overview(after), "job", "g1", "r2"), [university]
    )
    assert set(delta["collections"]) == {"cities"}
    assert "constructionEstimateBasis" not in delta["set"]
    moved = copy.deepcopy(before)
    moved["map"]["units"][0]["position"] = {"x": 1, "y": 0}
    assert changed_details(before, moved, [library, university]) == []


def test_startup_and_detail_refresh_use_existing_snapshot_without_extra_host_reads(tmp_path):
    async def run():
        host = snapshot()
        calls = []
        original = host.tool

        async def counted(name, args):
            calls.append(name)
            return await original(name, args)

        host.tool = counted
        io = ServiceIO()
        gateway = ProposalGateway(CivilizationState(tmp_path / "memory", "Rome"), io, tmp_path / "workspace")
        try:
            job = await gateway.open("job", "gameplay", "g1", host, {}, 0)
            startup = await prepare_startup(gateway, job)
            assert startup["reads"]["player_turn"]["cities"][0]["constructionEstimates"]
            before = list(calls)
            university = query(["University"])
            detail = await gateway.call(job, university.tool, university.arguments, "inspect")
            assert detail["data"]["constructions"][0]["completion"]["yields"]["change"]["Science"] == "6"
            assert calls == before
            assert university in job.details_read
            host.view["cities"][0]["constructionEstimates"]["University"] = estimate("8")
            host.revision = "rev2"
            await gateway.call(job, "player_turn", {}, "refresh")
            assert university not in job.details_read
            refreshed = await gateway.call(job, university.tool, university.arguments, "inspect-again")
            assert refreshed["data"]["constructions"][0]["completion"]["yields"]["change"]["Science"] == "8"
            assert calls.count("player_turn") == 2 and "construction_details" not in calls
        finally:
            await io.close()

    asyncio.run(run())


def test_locked_victory_construction_costs_are_named_planning_details_not_legal_commands():
    view = snapshot().view
    city = view["cities"][0]
    locked: JsonObject = estimate()
    locked.update(
        requiredTechnologies=["Robotics"],
        resourceRequirements={"Aluminum": 1},
        queueUnavailableReasons=["Requires Robotics"],
        completion=None,
    )
    city["constructionEstimates"]["SS Booster"] = locked
    named = project_detail(view, query(["SS Booster"]))["data"]["constructions"][0]
    assert named["production"] == locked["production"]
    assert named["requiredTechnologies"] == ["Robotics"]
    assert named["resourceRequirements"] == {"Aluminum": 1}
    assert not named["canQueue"] and named["purchase"] is None
    assert "command" not in json.dumps(named)
    assert "SS Booster" not in [row["name"] for row in project_detail(view, query())["data"]["constructions"]]
    assert "SS Booster" not in overview(view)["cities"][0]["constructionEstimates"]


def test_common_horizon_compares_native_conversion_losses_and_completion_returns():
    native = estimate()
    result = investment_comparison(native, {"available": True, "goldCost": 200}, 10)
    assert result["continueCurrent"] == {"Science": 110}
    assert result["build"]["yieldDifferenceOverHorizon"] == {"Science": 5}  # -1*5 + 2*5
    assert result["build"]["breakEvenTurnsByStat"] == {"Science": 8}
    assert result["purchase"]["yieldDifferenceOverHorizon"] == {"Science": 20}
    assert result["purchase"]["goldCostNow"] == 200
    assert investment_comparison(native, None, 3)["build"]["yieldDifferenceOverHorizon"] == {"Science": -3}
    assert native == estimate()


def test_comparison_preserves_blockers_and_does_not_invent_completion_benefits():
    native = estimate()
    native["queueUnavailableReasons"] = ["Requires Education"]
    result = investment_comparison(native, {"available": False, "goldCost": 200}, 10)
    assert result["build"] == {"available": False, "reasons": ["Requires Education"]}
    assert result["purchase"] == {"available": False}
    assert not investment_comparison({**native, "completion": None}, None, 10)["available"]
    view = snapshot().view
    selected = DetailQuery(
        tool="construction_details", arguments={"target": "city1", "names": ["Library"], "horizonTurns": 10}
    )
    assert project_detail(view, selected)["data"]["constructions"][0]["investmentComparison"]["horizonTurns"] == 10


def test_happiness_is_a_level_not_an_accumulating_yield():
    native = estimate()
    native["whileBuilding"]["before"]["Happiness"] = "4"
    native["whileBuilding"]["after"]["Happiness"] = "4"
    native["completion"]["yields"]["after"]["Happiness"] = "6"
    result = investment_comparison(native, {"available": True, "goldCost": 200}, 20)
    assert result["happiness"] == {"current": 4, "afterCompletion": 6, "difference": 2}
    assert "Happiness" not in result["continueCurrent"]
    assert "Happiness" not in result["build"]["yieldDifferenceOverHorizon"]
    assert "Happiness" not in result["purchase"]["yieldDifferenceOverHorizon"]


def test_followup_conversion_uses_native_rates_and_retains_unfinished_assets():
    native = estimate(turns=5)
    native["completion"]["following"] = {
        "Science": {"before": {"Science": "11"}, "after": {"Science": "15"}, "change": {"Science": "4"}}
    }
    result = investment_comparison(native, {"available": True, "goldCost": 200}, 10)
    scenario = result["following"]["Science"]
    assert scenario["continueCurrent"] == {"Science": 110}
    assert scenario["build"]["yieldDifferenceOverHorizon"] == {"Science": 15}  # -1*5 + 4*5
    assert scenario["purchase"]["yieldDifferenceOverHorizon"] == {"Science": 40}
    early = investment_comparison(native, None, 3)["build"]["endOfHorizon"]
    assert not early["completed"] and early["storedProduction"] == 40
    assert early["turnsRemainingAtCurrentRate"] == 2
    assert result["build"]["endOfHorizon"]["completed"]


def test_existing_queue_entry_can_resume_even_when_another_copy_cannot_be_appended():
    native = estimate()
    native.update(
        queueUnavailableReasons=["Cannot add this item to the current construction queue"], buildUnavailableReasons=[]
    )
    progress = [{"index": 1, "moveToIndices": [0], "name": "Library"}]
    result = investment_comparison(native, None, 10, progress=progress, can_queue=False)
    assert result["build"]["available"] and not result["queueOptions"]["canAppend"]
    assert result["queueOptions"]["buildNow"] == {"kind": "MOVE_CONSTRUCTION", "index": 1, "toIndex": 0}
    current = investment_comparison(native, None, 10, progress=[{"index": 0}], can_queue=False)
    assert current["build"]["available"] and current["queueOptions"]["buildNow"]["kind"] == "CONTINUE"
    native["buildUnavailableReasons"] = ["Requires Education"]
    assert not investment_comparison(native, None, 10, progress=progress, can_queue=False)["build"]["available"]


def test_named_blocked_conversion_explains_operator_era_restriction():
    view = snapshot().view
    view["constructionCatalogue"].append("Science")
    view["productionPolicy"] = {
        "blockedConversions": ["Gold", "Science", "Culture"],
        "conversionUnlockEra": "Future era",
    }
    row = project_detail(view, query(["Science"]))["data"]["constructions"][0]
    assert not row["canQueue"] and "command" not in row
    assert row["queueUnavailableReasons"] == ["Production conversion is disabled until Future era"]
