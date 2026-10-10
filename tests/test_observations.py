import asyncio
import copy
import json

import pytest
from jsonschema import Draft202012Validator

from civ_agent.civilization_state import CivilizationState
from civ_agent.game_commands import COMMANDS
from civ_agent.observations import (
    DETAIL_MODELS,
    DetailQuery,
    DetailResult,
    TurnOverview,
    overview,
    project_detail,
    required_detail,
)
from civ_agent.proposal_gateway import ProposalGateway, public_result
from civ_agent.protocol import JsonObject
from civ_agent.service_io import ServiceIO
from tests.memory_helpers import stored_records
from tests.test_proposal_gateway import Host
from tests.test_structured_memory import create, current, strategy
from tests.test_tactical_review import reviewed


def query(tool, **arguments):
    return DetailQuery.model_validate({"tool": tool, "arguments": arguments})


def test_attack_choices_do_not_change_attack_allowance_and_abilities_count_only_available():
    snapshot = reviewed()
    units = snapshot["map"]["units"]
    unit = next(u for u in units if u["combat"]["attacks"])
    units.remove(unit)
    units.insert(0, unit)
    attack = unit["combat"]["attacks"][0]
    unit["combat"].update(attacks=[attack, copy.deepcopy(attack)], attacksRemaining=1)
    unit["greatPerson"] = {"abilities": [{"available": True}, {"available": False}]}
    city = snapshot["cities"][0]
    city.update(attacks=[attack, copy.deepcopy(attack)], attacksRemaining=1)
    result = overview(snapshot)
    assert result["units"][0]["attackOptionCount"] == 2
    assert result["units"][0]["attacksRemaining"] == 1
    assert result["units"][0]["availableGreatPersonAbilityCount"] == 1
    assert result["cities"][0]["attackOptionCount"] == 2
    assert result["cities"][0]["attacksRemaining"] == 1
    unit["combat"].update(attacks=[], attacksRemaining=0)
    assert overview(snapshot)["units"][0]["attacksRemaining"] == 0
    assert "availableAttacks" not in json.dumps(result)


def test_automation_status_is_preloaded_and_inspected_orders_have_commands():
    snapshot = reviewed()
    unit = snapshot["map"]["units"][0]
    unit["combat"]["order"].update(action="Explore", automated=True)
    unit["combat"]["orders"] = [
        {"name": "STOP_AUTOMATION", "available": True, "unavailableReasons": []},
        {"name": "AUTOMATE", "available": False, "unavailableReasons": ["Already automated"]},
    ]
    compact = overview(snapshot)["units"][0]
    assert compact["order"] == "Explore" and compact["automated"]
    detail = project_detail(snapshot, query("unit_details", target=unit["handle"], sections=["orders"]))
    orders = detail["data"]["entity"]["combat"]["orders"]
    assert orders[0]["command"] == {"kind": "SET_UNIT_ORDER", "target": unit["handle"], "selection": "STOP_AUTOMATION"}
    assert "command" not in orders[1]


def test_settlement_advice_is_compact_even_without_a_settler():
    snapshot = reviewed()
    snapshot["map"]["units"] = []
    candidates = [
        {"position": {"x": n, "y": 4}, "score": 100 - n, "nearestCity": "city1", "distance": 4 + n} for n in range(5)
    ]
    snapshot["settlement"] = {
        "status": "SITES_AVAILABLE",
        "eligibleSites": 5,
        "blockedSites": 30,
        "candidates": candidates,
        "settlers": [],
    }
    compact = overview(snapshot)
    assert compact["units"] == [] and compact["settlement"]["settlers"] == []
    assert [c["position"] for c in compact["settlement"]["candidates"]] == [c["position"] for c in candidates[:3]]
    assert compact["settlement"]["eligibleSites"] == 5
    assert len(snapshot["settlement"]["candidates"]) == 5


def test_worker_options_explicitly_describe_current_tile_and_link_visible_surroundings():
    snapshot = reviewed()
    worker = snapshot["map"]["units"][0]
    worker["worker"] = {"options": [], "currentJob": None}
    detail = project_detail(snapshot, query("unit_details", target=worker["handle"], sections=["work"]))
    scope = detail["data"]["workScope"]
    assert scope["kind"] == "CURRENT_TILE" and scope["position"] == worker["position"]
    surrounding = project_detail(snapshot, DetailQuery.model_validate(scope["surroundings"]))
    assert surrounding["available"]
    movement = project_detail(snapshot, query("unit_details", target=worker["handle"], sections=["movement"]))
    assert "workScope" not in movement["data"]


def test_compact_snapshot_keeps_decisions_and_all_details_remain_reachable():
    snapshot = reviewed()
    snapshot["alerts"] = [{"handle": "alert1", "kind": "WonderBuilt", "content": {"paragraphs": ["Important " * 1000]}}]
    snapshot["socialAdvisories"] = [
        {"proposal": "p1", "terms": "No attacks before turn 8", "conditions": "Unconditional"}
    ]
    compact = overview(snapshot)
    TurnOverview.model_validate(compact)
    assert compact["alerts"] == snapshot["alerts"]
    assert compact["combatReports"] == snapshot["combatReports"]
    assert compact["tacticalReview"] == snapshot["tacticalReview"]
    assert compact["socialAdvisories"] == snapshot["socialAdvisories"]
    assert "units" not in compact["turnReview"]
    assert "map" not in compact and "purchases" not in compact["cities"][0]
    assert "moves" not in compact["units"][0]
    for name, entities in (("unit_details", snapshot["map"]["units"]), ("city_details", snapshot["cities"])):
        for entity in entities:
            detail = project_detail(snapshot, query(name, target=entity["handle"]))
            DetailResult.model_validate(detail)
            assert detail["available"] and detail["data"]["entity"] == entity
            detail["data"]["entity"]["name"] = "No shared mutable state"
            assert entity["name"] != "No shared mutable state"
    full_bytes = len(json.dumps(snapshot))
    compact_bytes = len(json.dumps(compact))
    assert compact_bytes < full_bytes * 0.7, (full_bytes, compact_bytes)


def test_named_constructions_preserve_confirmations_and_unavailable_catalogue_entries():
    snapshot = reviewed()
    snapshot["constructionCatalogue"].append("Future Wonder")
    city = snapshot["cities"][0]
    purchase = {
        "name": "Future Wonder",
        "description": "Unique effect",
        "available": False,
        "unavailableReasons": ["Requires technology"],
        "goldCost": None,
        "confirmation": "Consequences",
    }
    city["purchases"] = {"constructions": [purchase]}
    catalogue = project_detail(snapshot, query("construction_details", target=city["handle"]))
    assert "Future Wonder" in [c["name"] for c in catalogue["data"]["constructions"]]
    named = project_detail(snapshot, query("construction_details", target=city["handle"], names=["Future Wonder"]))
    assert named["data"]["constructions"][0]["purchase"] == purchase
    assert not named["data"]["constructions"][0]["canQueue"]
    command = {"kind": "BUY_CONSTRUCTION", "target": city["handle"], "selection": "Future Wonder"}
    assert required_detail(command) is None


def test_conversion_can_be_compared_before_queueing_and_only_current_output_is_preloaded():
    snapshot = reviewed()
    snapshot["constructionCatalogue"].extend(["Science", "Gold"])
    city = snapshot["cities"][0]
    city["constructionOptions"] = ["Science", "Gold"]
    city["constructionQueue"] = ["Worker"]
    conversion = {"stat": "Science", "productionPerTurn": "20", "baseYieldPerProduction": 0.25, "yieldPerTurn": "9.2"}
    city["productionConversions"] = {
        "Science": conversion,
        "Gold": {**conversion, "stat": "Gold", "yieldPerTurn": "5"},
    }
    named = project_detail(snapshot, query("construction_details", target=city["handle"], names=["Science"]))
    entry = named["data"]["constructions"][0]
    assert entry["conversion"] == conversion
    assert entry["canQueue"] and entry["progress"] == []
    assert entry["command"]["selection"] == "Science"
    entry["conversion"]["yieldPerTurn"] = "999"
    assert conversion["yieldPerTurn"] == "9.2"
    catalogue = project_detail(snapshot, query("construction_details", target=city["handle"]))
    assert next(c for c in catalogue["data"]["constructions"] if c["name"] == "Science")["conversion"] == conversion
    production = project_detail(snapshot, query("city_details", target=city["handle"], sections=["production"]))
    assert production["data"]["entity"]["productionConversions"] == city["productionConversions"]
    citizens = project_detail(snapshot, query("city_details", target=city["handle"], sections=["citizens"]))
    assert "productionConversions" not in citizens["data"]["entity"]
    assert "constructionConversion" not in overview(snapshot)["cities"][0]
    city["constructionQueue"] = ["Science"]
    assert overview(snapshot)["cities"][0]["constructionConversion"] == conversion
    assert "productionConversions" not in overview(snapshot)["cities"][0]


def test_wonder_benefits_and_blockers_are_shared_while_named_reads_retain_production_accounting():
    snapshot = reviewed()
    snapshot["constructionCatalogue"].append("Oxford University")
    city = snapshot["cities"][0]
    city["constructionOptions"] = []
    option = {
        "city": city["handle"],
        "canQueue": False,
        "queued": False,
        "productionCost": 164,
        "productionStored": 0,
        "productionPerTurn": 32,
        "turns": 6,
        "unavailableReasons": ["Requires a University in all non-Puppeted cities"],
    }
    estimate = {
        "production": {"cost": 164, "stored": 0, "overflow": 0, "perTurn": 32, "turnsIfStartedNow": 6},
        "queueUnavailableReasons": option["unavailableReasons"],
        "whileBuilding": {"change": {}},
        "completion": None,
    }
    city["constructionEstimates"] = {"Oxford University": estimate}
    snapshot["wonderOpportunities"] = [
        {
            "name": "Oxford University",
            "kind": "NATIONAL",
            "benefits": "+3 Science; Free Technology",
            "greatPersonPoints": {},
            "specialistSlots": {},
            "cities": [option, {**option, "city": "city2", "turns": 12}],
        }
    ]
    compact = overview(snapshot)["wonderOpportunities"][0]
    assert compact == {
        "name": "Oxford University",
        "kind": "NATIONAL",
        "benefits": "+3 Science; Free Technology",
        "productionCost": 164,
        "cities": [],
        "unavailableCities": [{"cities": [city["handle"], "city2"], "reasons": option["unavailableReasons"]}],
    }
    named = project_detail(snapshot, query("construction_details", target=city["handle"], names=["Oxford University"]))
    assert not named["data"]["constructions"][0]["canQueue"]
    assert named["data"]["constructions"][0]["production"] == estimate["production"]
    assert named["data"]["constructions"][0]["queueUnavailableReasons"] == option["unavailableReasons"]
    option.update(canQueue=True, unavailableReasons=[])
    estimate["queueUnavailableReasons"] = []
    city["constructionOptions"] = ["Oxford University"]
    compact = overview(snapshot)["wonderOpportunities"][0]
    assert compact["cities"] == [{"city": city["handle"], "turns": 6, "productionStored": 0, "productionPerTurn": 32}]
    assert compact["unavailableCities"][0]["cities"] == ["city2"]
    catalogue = project_detail(snapshot, query("construction_details", target=city["handle"]))
    assert catalogue["data"]["constructions"][0]["production"] == estimate["production"]


def test_map_regions_respect_wrap_fog_and_observed_coordinates():
    snapshot: JsonObject = {
        "turn": 0,
        "map": {
            "wrapOffset": {"x": 10, "y": -10},
            "tiles": [
                {"position": {"x": 5, "y": -5}, "visible": True, "terrain": "Grassland"},
                {"position": {"x": -4, "y": 5}, "visible": False, "terrain": "Hill"},
                {"position": {"x": 0, "y": 0}, "visible": True, "terrain": "Desert"},
            ],
            "frontier": [{"x": 5, "y": -4}],
        },
    }
    region = project_detail(snapshot, query("map_region", center={"x": 5, "y": -5}, radius=1))
    assert [t["terrain"] for t in region["data"]["tiles"]] == ["Grassland", "Hill"]
    assert region["data"]["frontier"] == [{"x": 5, "y": -4}]
    assert not project_detail(snapshot, query("map_region", center={"x": 100, "y": 100}))["available"]
    snapshot["map"]["wrapOffset"] = None
    assert len(project_detail(snapshot, query("map_region", center={"x": 5, "y": -5}, radius=1))["data"]["tiles"]) == 1


def test_detail_tools_cache_reads_require_refresh_and_cannot_acknowledge_unread_terms(tmp_path):
    async def run():
        memory, io, host = CivilizationState(tmp_path / "private", "Rome"), ServiceIO(), Host()
        gateway = ProposalGateway(memory, io, tmp_path / "workspace")
        calls = []
        original = host.tool

        async def counted(name, args):
            calls.append(name)
            value = await original(name, args)
            if name == "player_communication" and args["op"] == "state":
                value["offers"] = [{"proposal": "p1", "version": 1, "terms": "No attacks"}]
            if name == "player_command" and args.get("kind") == "MOVE_UNIT":
                host.view["map"]["units"][0]["position"] = args["destination"]
            return value

        host.tool = counted
        job = await gateway.open("game", "gameplay", "g1", host, {}, 0)
        try:
            assert (await gateway.call(job, "unit_details", {"target": "unit1"}, "early"))[
                "error"
            ] == "OBSERVATION_REQUIRED"
            shown = await gateway.call(job, "player_turn", {}, "overview")
            assert "No attacks" not in str(shown)
            for source in ("observation:unit/unit1", "observation:game", "observation:communication"):
                rejected = memory.patch(
                    job.id,
                    [
                        {
                            "op": "create",
                            "content": {
                                "type": "belief",
                                "claim": "Unverified report",
                                "uncertainty": "high",
                                "sources": [source],
                            },
                        }
                    ],
                )
                assert rejected["changes"][0]["accepted"]
                assert rejected["changes"][0]["warnings"][0]["sources"] == [source]
            unread = await gateway.call(
                job,
                "commitment_decide",
                {"proposalId": "p1", "decision": "accept", "intent": {"reason": "Agree to these terms"}},
                "unread",
            )
            assert unread["error"] == "PROPOSAL_NOT_OBSERVED" and unread["applied"] is False
            assert unread["recovery"]["reads"] == [{"tool": "chat_read", "arguments": {}}]
            action = {"kind": "MOVE_UNIT", "target": "unit1", "destination": {"x": 1, "y": 0}}
            first = await gateway.call(job, "player_command", action, "missing")
            assert first["done"] and len(host.executed) == 1
            before = len(calls)
            detail = await gateway.call(job, "unit_details", {"target": "unit1"}, "detail")
            assert len(calls) == before
            tools = {t["name"]: t for t in await gateway.tools(job)}
            assert Draft202012Validator(tools["unit_details"]["outputSchema"]).is_valid(detail)
            create(memory, strategy(sources=[detail["evidence"]]), job=job.id)
            assert await gateway.call(job, "player_command", action, "missing") == first
            receipt = await gateway.call(job, "player_command", action, "move")
            assert receipt["done"] and len(host.executed) == 2
            assert receipt["stateUpdate"]["kind"] == "delta"
            assert (await gateway.call(job, "player_command", action, "again"))["done"]
            assert await gateway.call(job, "player_command", action, "move") == receipt
            await gateway.call(job, "unit_details", {"target": "unit1"}, "updated")
            assert (await gateway.call(job, "player_command", action, "move-fresh"))["done"]
        finally:
            await gateway.close(job, True)
            await io.close()

    asyncio.run(run())


def test_only_changed_detail_invalidates_its_assumptions_and_survives_restart(tmp_path):
    async def run():
        memory, io, host = CivilizationState(tmp_path / "private", "Rome"), ServiceIO(), Host()
        gateway = ProposalGateway(memory, io, tmp_path / "workspace")
        job = await gateway.open("game", "gameplay", "g1", host, {}, 0)
        try:
            await gateway.call(job, "player_turn", {}, "read")
            detail = await gateway.call(job, "unit_details", {"target": "unit1"}, "inspect")
            key = create(
                memory, strategy(assumptions=[{"text": "Unit is healthy", "source": detail["evidence"]}]), job=job.id
            )
            host.view["cities"][0]["name"] = "Roma"
            host.revision = "renamed"
            await gateway.call(job, "player_turn", {}, "unrelated")
            assert not current(memory, key)["needsReview"]
            host.view["map"]["units"][0]["health"] = 50
            host.revision = "injured"
            await gateway.call(job, "player_turn", {}, "affected")
            assert current(memory, key)["needsReview"]
            await gateway.close(job, True)
            restarted = CivilizationState(tmp_path / "private", "Rome")
            assert current(restarted, key)["needsReview"]
            restarted.start_job("next", "gameplay", {}, 1)
            snapshot = await host.tool("player_turn", {})
            snapshot["map"]["units"] = []
            restarted.observe_player(snapshot, "next", overview(snapshot))
            canonical = current(memory, key)["_assumptions"][0]["key"]
            stored = stored_records(restarted)[canonical]["body"]
            assert not stored["available"] and stored["data"] == {}
        finally:
            await io.close()

    asyncio.run(run())


@pytest.mark.parametrize(
    "name,arguments,source",
    [
        ("player_turn", {}, "observation:overview"),
        ("trade_options", {"partner": "Greece"}, "observation:trade_options/Greece"),
        ("trade_read", {"proposalId": "d1"}, "observation:trade/d1"),
    ],
)
def test_social_advisories_are_shown_without_staling_native_evidence(tmp_path, name, arguments, source):
    async def run():
        memory, io, host = CivilizationState(tmp_path / "private", "Rome"), ServiceIO(), Host()
        gateway = ProposalGateway(memory, io, tmp_path / "workspace")
        original = host.tool
        advisories = []
        host.view["socialAdvisories"] = advisories
        host.offers = [
            {
                "proposal": "p1",
                "version": 1,
                "participants": ["Rome", "Greece"],
                "kind": "peace",
                "terms": "No attacks before turn 8",
                "effective": True,
            }
        ]

        async def with_trade_advisories(name, args):
            if name == "player_communication" and args["op"] == "trade_options":
                return {
                    "mode": "SHARED",
                    "give": [{"type": "WarDeclaration", "war": {"socialAdvisories": copy.deepcopy(advisories)}}],
                }
            if name == "player_communication" and args["op"] == "trade_read":
                return {
                    "trade": {
                        "proposal": "d1",
                        "version": 1,
                        "mode": "SHARED",
                        "socialAdvisories": copy.deepcopy(advisories),
                    },
                    "history": [{"version": 1, "socialAdvisories": copy.deepcopy(advisories)}],
                }
            return await original(name, args)

        host.tool = with_trade_advisories
        game = await gateway.open("game", "gameplay", "g1", host, {})
        chat = await gateway.open("chat", "communication", "g1", host, {})
        try:
            await gateway.call(game, "player_turn", {}, "turn")
            await gateway.call(game, "unit_details", {"target": "unit1"}, "unit")
            await gateway.call(game, name, arguments, "initial")
            key = create(
                memory,
                strategy(assumptions=[{"text": "The native situation is unchanged", "source": source}]),
                job=game.id,
            )
            before = stored_records(memory)[source]
            advisories.append(
                {
                    "proposal": "p1",
                    "version": 1,
                    "participants": ["Rome", "Greece"],
                    "terms": "No attacks before turn 8",
                    "conditions": "Unconditional",
                    "relevantActions": ["ATTACK", "DECLARE_WAR"],
                    "message": "Consider this promise when choosing; legal actions remain available.",
                }
            )
            shown = await gateway.call(chat, name, arguments, "advisory")
            assert "Consider this promise" in json.dumps(shown)
            assert stored_records(memory)[source] == before
            assert not current(memory, key)["needsReview"]
            # The gameplay job can still use its native evidence and chosen strategy
            # without reading the annotation or declaring a social override.
            result = await gateway.call(
                game,
                "player_command",
                {
                    "kind": "ATTACK",
                    "target": "unit1",
                    "destination": {"x": 1, "y": 0},
                    "attackFrom": {"x": 0, "y": 0},
                    "intent": {"strategy": key, "reason": "Defend Rome"},
                },
                "act",
            )
            assert result["done"]
            # Terms remain available as ordinary communication evidence for
            # remembering or discussing the promise itself.
            await gateway.call(chat, "chat_read", {}, "ledger")
            social = create(memory, strategy(sources=["observation:communication"]), job=chat.id)
            assert current(memory, social)["_evidence"][0]["key"] == "observation:communication"
            assert stored_records(memory)["observation:communication"]["body"]["offers"] == host.offers
        finally:
            await gateway.close(game, True)
            await gateway.close(chat, True)
            await io.close()

    asyncio.run(run())


def test_detail_input_schemas_and_every_command_have_a_deliberate_read_path():
    for name, model in DETAIL_MODELS.items():
        assert not Draft202012Validator(model.model_json_schema()).is_valid({"unexpected": True}), name
    assert not Draft202012Validator(DETAIL_MODELS["map_region"].model_json_schema()).is_valid(
        {"center": {"x": 0, "y": 0}, "radius": 5}
    )
    preview_commands = {
        "SET_UNIT_DESTINATION",
        "REBASE_UNIT",
        "PARADROP",
        "AIR_SWEEP",
        "NUCLEAR_STRIKE",
        "CONNECT_ROAD",
    }
    schema = COMMANDS.json_schema()
    for kind, ref in schema["discriminator"]["mapping"].items():
        properties = schema["$defs"][ref.rsplit("/", 1)[1]]["properties"]
        command = {"kind": kind, "selection": "Worker", **({"target": "id"} if "target" in properties else {})}
        if "destination" in properties:
            command["destination"] = {"x": 6, "y": 0}
        assert (required_detail(command) is not None) == (kind in preview_commands), kind
    assert public_result({"epoch": "secret", "data": {"revision": "internal", "health": 100}}) == {
        "data": {"health": 100}
    }
