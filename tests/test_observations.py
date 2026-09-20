import asyncio
import copy
import json

import pytest
from jsonschema import Draft202012Validator

from civ_agent.civilization_state import CivilizationState, StateConflict
from civ_agent.game_commands import COMMANDS
from civ_agent.observations import (
    DETAIL_MODELS,
    DetailQuery,
    DetailResult,
    TurnOverview,
    detail_satisfies,
    overview,
    project_detail,
    required_detail,
)
from civ_agent.proposal_gateway import ProposalGateway, public_result
from civ_agent.protocol import JsonObject
from civ_agent.service_io import ServiceIO
from tests.test_proposal_gateway import Host
from tests.test_structured_memory import create, current, plan
from tests.test_tactical_review import reviewed


def query(tool, **arguments):
    return DetailQuery.model_validate({"tool": tool, "arguments": arguments})


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
    assert compact["turnReview"]["units"] == (snapshot.get("turnReview") or {}).get("units", [])
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
    required = required_detail({"kind": "BUY_CONSTRUCTION", "target": city["handle"], "selection": "Future Wonder"})
    assert required is not None
    assert not detail_satisfies(DetailQuery.model_validate(catalogue["query"]), required)
    assert detail_satisfies(DetailQuery.model_validate(named["query"]), required)


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
                rejected = memory.patch(job.id, [{"op": "create", "content": plan(sources=[source])}])
                assert not rejected["changes"][0]["accepted"]
                assert "SOURCE_NOT_DELIVERED" in str(rejected)
            with pytest.raises(StateConflict, match="chat_read"):
                await gateway.call(
                    job,
                    "commitment_decide",
                    {"proposal": "p1", "decision": "accept", "intent": {"reason": "Agree to these terms"}},
                    "unread",
                )
            action = {"kind": "MOVE_UNIT", "target": "unit1", "destination": {"x": 1, "y": 0}}
            rejected = await gateway.call(job, "player_command", action, "missing")
            assert rejected["error"] == "DETAIL_REQUIRED" and not host.executed
            before = len(calls)
            detail = await gateway.call(job, "unit_details", {"target": "unit1"}, "detail")
            assert len(calls) == before
            tools = {t["name"]: t for t in await gateway.tools(job)}
            assert Draft202012Validator(tools["unit_details"]["outputSchema"]).is_valid(detail)
            create(memory, plan(sources=[detail["evidence"]]), job=job.id)
            assert await gateway.call(job, "player_command", action, "missing") == rejected
            receipt = await gateway.call(job, "player_command", action, "move")
            assert receipt["done"] and len(host.executed) == 1
            assert (await gateway.call(job, "unit_details", {"target": "unit1"}, "stale"))[
                "error"
            ] == "OBSERVATION_REQUIRED"
            await gateway.call(job, "player_turn", {}, "refresh")
            assert (await gateway.call(job, "player_command", action, "again"))["error"] == "DETAIL_REQUIRED"
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
                memory, plan(assumptions=[{"text": "Unit is healthy", "source": detail["evidence"]}]), job=job.id
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
            stored = restarted.snapshot("gameplay")["records"][detail["evidence"]]["body"]
            assert not stored["available"] and stored["data"] == {}
        finally:
            await io.close()

    asyncio.run(run())


@pytest.mark.parametrize(
    "name,arguments,source",
    [
        ("player_turn", {}, "observation:overview"),
        ("trade_options", {"partner": "Greece"}, "observation:trade_options/Greece"),
        ("trade_read", {"proposal": "d1"}, "observation:trade/d1"),
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
                return {"give": [{"type": "WarDeclaration", "war": {"socialAdvisories": copy.deepcopy(advisories)}}]}
            if name == "player_communication" and args["op"] == "trade_read":
                return {
                    "trade": {"proposal": "d1", "version": 1, "socialAdvisories": copy.deepcopy(advisories)},
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
                plan(assumptions=[{"text": "The native situation is unchanged", "source": source}]),
                job=game.id,
            )
            before = memory.snapshot("gameplay")["records"][source]
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
            assert memory.snapshot("gameplay")["records"][source] == before
            assert not current(memory, key)["needsReview"]
            # The gameplay job can still use its native evidence and chosen plan
            # without reading the annotation or declaring a social override.
            result = await gateway.call(
                game,
                "player_command",
                {
                    "kind": "ATTACK",
                    "target": "unit1",
                    "destination": {"x": 1, "y": 0},
                    "attackFrom": {"x": 0, "y": 0},
                    "intent": {"plan": key},
                },
                "act",
            )
            assert result["done"]
            # Terms remain available as ordinary communication evidence for
            # remembering or discussing the promise itself.
            await gateway.call(chat, "chat_read", {}, "ledger")
            social = create(memory, plan(sources=["observation:communication"]), job=chat.id)
            assert current(memory, social)["_evidence"][0]["key"] == "observation:communication"
            assert (
                memory.snapshot("communication")["records"]["observation:communication"]["body"]["offers"]
                == host.offers
            )
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
    no_target_detail = {
        "END_TURN",
        "SET_RESEARCH_QUEUE",
        "CHOOSE_FREE_TECHNOLOGY",
        "ADOPT_POLICY",
        "DISMISS_POLICY_PICKER",
        "CHOOSE_FREE_GREAT_PERSON",
        "CAST_DIPLOMATIC_VOTE",
        "ABSTAIN_DIPLOMATIC_VOTE",
        "ACKNOWLEDGE_DIPLOMATIC_RESULTS",
        "ACKNOWLEDGE_ALERT",
        "DISMISS_NOTIFICATIONS",
        "RESPOND_FRIENDSHIP",
        "RESOLVE_CAPTURED_CITY",
    }
    schema = COMMANDS.json_schema()
    for kind, ref in schema["discriminator"]["mapping"].items():
        properties = schema["$defs"][ref.rsplit("/", 1)[1]]["properties"]
        command = {"kind": kind, "selection": "Worker", **({"target": "id"} if "target" in properties else {})}
        assert (required_detail(command) is None) == (kind in no_target_detail), kind
    assert public_result({"epoch": "secret", "data": {"revision": "internal", "health": 100}}) == {
        "data": {"health": 100}
    }
