"""Action inspections, delivered evidence, and argument recovery at the player boundary."""

import asyncio
import copy
import json

import pytest
from jsonschema import Draft202012Validator
from pydantic import ValidationError

from civ_agent.action_templates import add_decision_commands
from civ_agent.civilization_state import CivilizationState
from civ_agent.entity_sections import CityDetails, UnitDetails
from civ_agent.game_commands import command_arguments, command_error
from civ_agent.observations import DetailQuery, detail_satisfies, overview, project_detail, required_detail
from civ_agent.proposal_gateway import ProposalGateway
from civ_agent.protocol import JsonObject
from civ_agent.service_io import ServiceIO
from tests.test_proposal_gateway import Host
from tests.test_structured_memory import create, current, plan


def query(tool, target, sections):
    model = UnitDetails if tool == "unit_details" else CityDetails
    return DetailQuery(tool=tool, arguments=model(target=target, sections=sections).model_dump())


def options_snapshot():
    snapshot = copy.deepcopy(Host().view)
    snapshot["phase"] = "AWAITING_PLAYER"
    unit = snapshot["map"]["units"][0]
    unit.update(
        moves=[{"destination": {"x": 1, "y": 0}, "path": [{"x": 1, "y": 0}], "movementCost": "1"}],
        nearbyHostiles=[{"name": "Warrior", "distance": 2}],
        canFoundCity=True,
        settlement={
            "destination": unit["position"],
            "atDestination": True,
            "canFoundNow": True,
            "siteStatus": "ELIGIBLE",
            "siteBlockers": [],
            "unitBlockers": [],
            "uncertainty": None,
            "minimumCityDistance": 4,
            "minimumCityDistanceOnDifferentContinents": 3,
        },
        settlementPromises=[{"civilization": "Greece", "consequences": "Break native promise"}],
        combat={
            "promotions": {
                "choices": [{"name": "Drill I", "available": True}, {"name": "Drill II", "available": False}]
            }
        },
        economy={"disband": {"available": True, "gold": 10, "confirmation": "Lose unit"}},
        worker={
            "canStop": True,
            "options": [{"name": "Farm", "available": True}, {"name": "Mine", "available": False}],
        },
        waterImprovement={"name": "Fishing Boats", "available": True, "consumesUnit": True},
        cityStateGift={"civilizationId": "Geneva", "action": {"available": True}, "consumesUnit": True},
        greatPerson={"abilities": [{"name": "Discover technology", "available": True, "consumesUnit": True}]},
        spaceshipAssembly={"available": True, "consumesUnit": True},
        futureInformation={"visible": True},
    )
    city = snapshot["cities"][0]
    city.update(
        development={
            "position": {"x": 0, "y": 0},
            "population": 3,
            "inResistance": False,
            "yields": {"Gold": 4},
            "avoidGrowth": False,
            "constructions": [{"index": 0, "name": "Market", "canRemove": True, "moveToIndices": [1]}],
            "tiles": [{"position": {"x": 1, "y": 0}, "canWork": True, "canUnlock": True}],
        },
        management={
            "avoidGrowth": {"available": True},
            "manualSpecialists": False,
            "specialistControl": {"available": True},
            "resetCitizens": {"available": True},
            "buildingSales": [
                {"name": "Monument", "gold": 10, "action": {"available": True}, "confirmation": "Lose culture"}
            ],
            "specialists": [{"name": "Scientist", "assign": {"available": True}, "unassign": {"available": False}}],
        },
        purchases={
            "constructions": [
                {"name": "Market", "available": True, "goldCost": 400, "confirmation": "Spend 400 gold"},
                {"name": "Granary", "available": False, "unavailableReasons": ["Insufficient gold"]},
            ],
            "tiles": [{"position": {"x": 1, "y": 0}, "available": True, "goldCost": 50, "confirmation": "Spend gold"}],
        },
    )
    return snapshot


def test_selective_reads_preserve_consequences_without_delivering_unrelated_fields():
    snapshot = options_snapshot()
    before = copy.deepcopy(snapshot)
    move = project_detail(snapshot, query("unit_details", "unit1", ["movement"]))["data"]["entity"]
    assert move["moves"][0]["path"] and move["nearbyHostiles"]
    assert move["moves"][0]["command"] == {"kind": "MOVE_UNIT", "target": "unit1", "destination": {"x": 1, "y": 0}}
    assert (
        not {"economy", "worker", "combat", "cityStateGift", "settlementPromises", "settlement", "futureInformation"}
        & move.keys()
    )
    settlement = project_detail(snapshot, query("unit_details", "unit1", ["settlement"]))["data"]["entity"]
    assert settlement["settlementPromises"] == before["map"]["units"][0]["settlementPromises"]
    assert settlement["settlement"] == before["map"]["units"][0]["settlement"]
    assert settlement["foundCityCommand"] == {"kind": "FOUND_CITY", "target": "unit1"}
    assert settlement["foundCityConfirmationField"] == "confirmBreakPromise"
    economy = project_detail(snapshot, query("city_details", "city1", ["economy"]))["data"]["entity"]
    sale = economy["management"]["buildingSales"][0]
    assert (sale["confirmation"], sale["confirmationField"]) == ("Lose culture", "confirmAction")
    assert "confirmAction" not in sale["command"]
    assert "purchases" not in economy and "tiles" not in economy["development"]
    full = project_detail(snapshot, query("unit_details", "unit1", ["all"]))["data"]["entity"]
    assert full["futureInformation"] == {"visible": True}
    assert len(json.dumps(move)) < len(json.dumps(full)) / 2
    assert snapshot == before
    assert overview(snapshot)["units"][0]["availablePromotions"] == 1


@pytest.mark.parametrize("status", ["BLOCKED", "UNKNOWN", "ELIGIBLE"])
def test_unavailable_founding_still_offers_inspection_without_exposing_full_details(status):
    snapshot = options_snapshot()
    unit = snapshot["map"]["units"][0]
    unit["canFoundCity"] = False
    unit["settlement"].update(
        canFoundNow=False,
        siteStatus=status,
        siteBlockers=[{"code": "CITY_TOO_CLOSE", "city": "Rome", "distance": 3, "minimumDistance": 4}]
        if status == "BLOCKED"
        else [],
        unitBlockers=[{"code": "NO_MOVEMENT"}],
    )
    shown = overview(snapshot)["units"][0]
    assert shown["settlementStatus"] == status
    assert not shown["canFoundCity"] and "settlement" not in shown
    assert shown["inspections"]["FOUND_CITY"] == {
        "tool": "unit_details",
        "arguments": {"target": "unit1", "sections": ["settlement"]},
    }
    detail = project_detail(snapshot, query("unit_details", "unit1", ["settlement"]))["data"]["entity"]
    assert detail["settlement"] == unit["settlement"]
    assert "foundCityCommand" not in detail
    assert "movement_preview" in detail["settlementInstruction"]


@pytest.mark.parametrize(
    "kind,section",
    [
        ("DISBAND_UNIT", "economy"),
        ("GIFT_UNIT", "abilities"),
        ("FOUND_CITY", "settlement"),
        ("PROMOTE_UNIT", "promotions"),
    ],
)
def test_movement_cannot_stand_in_for_consequential_inspections(kind, section):
    needed = required_detail({"kind": kind, "target": "unit1"})
    assert needed is not None
    assert not detail_satisfies(query("unit_details", "unit1", ["movement"]), needed)
    assert not detail_satisfies(query("unit_details", "different-unit", [section]), needed)
    assert detail_satisfies(query("unit_details", "unit1", [section]), needed)
    assert detail_satisfies(query("unit_details", "unit1", ["all"]), needed)


def test_production_alone_does_not_authorize_a_purchase_or_unread_construction():
    needed = required_detail({"kind": "BUY_CONSTRUCTION", "target": "city1", "selection": "Market"})
    assert needed is not None
    assert not detail_satisfies(query("city_details", "city1", ["production"]), needed)
    assert detail_satisfies(query("city_details", "city1", ["production", "purchases"]), needed)
    assert not detail_satisfies(
        DetailQuery(tool="construction_details", arguments={"target": "city1", "names": ["Granary"]}), needed
    )


def test_sections_are_canonical_and_invalid_selections_are_rejected():
    assert query("unit_details", "unit1", ["movement", "combat"]) == query(
        "unit_details", "unit1", ["combat", "movement", "combat"]
    )
    assert query("unit_details", "unit1", ["movement", "all"]) == query("unit_details", "unit1", ["all"])
    assert UnitDetails(target="unit1").sections == ["all"]
    for model in (UnitDetails, CityDetails):
        for sections in ([], ["invisible"], "all"):
            with pytest.raises(ValidationError):
                model.model_validate({"target": "id", "sections": sections})


def commands_in(value):
    if isinstance(value, dict):
        if "kind" in value:
            yield value
        else:
            for part in value.values():
                yield from commands_in(part)
    elif isinstance(value, list):
        for part in value:
            yield from commands_in(part)


def test_offered_templates_validate_and_never_supply_consent_or_intent():
    snapshot = options_snapshot()
    unit = project_detail(snapshot, query("unit_details", "unit1", ["all"]))["data"]["entity"]
    city = project_detail(snapshot, query("city_details", "city1", ["all"]))["data"]["entity"]
    named = project_detail(
        snapshot,
        DetailQuery(tool="construction_details", arguments={"target": "city1", "names": ["Market", "Granary"]}),
    )
    commands = list(commands_in([unit, city, named]))
    assert {c["kind"] for c in commands} >= {
        "MOVE_UNIT",
        "FOUND_CITY",
        "PROMOTE_UNIT",
        "QUEUE_CONSTRUCTION",
        "BUY_CONSTRUCTION",
        "DISBAND_UNIT",
        "GIFT_UNIT",
        "START_WORKER_JOB",
        "STOP_WORKER_JOB",
        "CREATE_WATER_IMPROVEMENT",
        "ADD_SPACESHIP_PART",
        "ACTIVATE_GREAT_PERSON",
        "REMOVE_CONSTRUCTION",
        "MOVE_CONSTRUCTION",
        "SET_WORKED_TILE",
        "SET_TILE_LOCKED",
        "SET_AVOID_GROWTH",
        "SET_MANUAL_SPECIALISTS",
        "ASSIGN_SPECIALIST",
        "RESET_CITIZENS",
        "SELL_BUILDING",
        "BUY_TILE",
    }
    for command in commands:
        assert command_arguments(command) == command
        assert not {"intent", "confirmAction", "confirmBreakPromise", "confirmWar"} & command.keys()
    assert "command" not in unit["worker"]["options"][1]
    assert "command" not in unit["combat"]["promotions"]["choices"][1]
    assert "command" not in city["purchases"]["constructions"][1]
    assert "command" not in city["management"]["specialists"][0]["unassign"]
    assert named["data"]["constructions"][0]["purchase"]["confirmation"] == "Spend 400 gold"
    catalogue = project_detail(snapshot, DetailQuery(tool="construction_details", arguments={"target": "city1"}))
    assert not list(commands_in(catalogue))


def test_research_templates_preserve_native_prerequisite_paths_and_free_choices():
    value: JsonObject = {
        "technologies": [
            {
                "name": "Bronze Working",
                "queueAvailable": True,
                "prerequisiteQueue": ["Mining", "Bronze Working"],
                "researchNow": {"available": False},
            },
            {"name": "Pottery", "researchNow": {"available": True}, "freeChoice": {"available": True}},
            {"name": "Unknown tech", "queueAvailable": False, "researchNow": {"available": False}},
        ]
    }
    add_decision_commands("research_details", value, {})
    assert not list(commands_in(value))
    add_decision_commands("research_details", value, {"names": ["Bronze Working", "Pottery", "Unknown tech"]})
    assert value["technologies"][0]["command"] == {"kind": "SET_RESEARCH_QUEUE", "queue": ["Mining", "Bronze Working"]}
    assert value["technologies"][1]["freeChoice"]["command"] == {
        "kind": "CHOOSE_FREE_TECHNOLOGY",
        "selection": "Pottery",
    }
    assert "command" not in value["technologies"][2]
    for command in commands_in(value):
        assert command_arguments(command) == command


@pytest.mark.parametrize(
    "queue,path,appended",
    [
        (["Mining"], ["Calendar"], ["Mining", "Calendar"]),
        (["Mining", "Bronze Working"], ["Pottery", "Calendar"], ["Mining", "Bronze Working", "Pottery", "Calendar"]),
        (["Mining"], ["Mining", "Bronze Working"], None),
        (["Mining", "Bronze Working"], ["Mining"], None),
        ([], ["Calendar"], None),
    ],
)
def test_research_choices_distinguish_replacement_from_preserving_the_whole_queue(queue, path, appended):
    value: JsonObject = {
        "researchQueue": queue,
        "technologies": [{"name": path[-1], "queueAvailable": True, "prerequisiteQueue": path}],
    }
    add_decision_commands("research_details", value, {"names": [path[-1]]})
    choice = value["technologies"][0]
    assert choice["command"]["queue"] == path
    assert "Replace queue" in choice["commandLabel"]
    if appended:
        assert choice["appendToQueue"]["command"] == {"kind": "SET_RESEARCH_QUEUE", "queue": appended}
    else:
        assert "appendToQueue" not in choice
    assert choice.get("alreadyQueued", False) == (path[-1] in queue)


@pytest.mark.parametrize(
    "mission,kind",
    [
        (None, "SET_UNIT_DESTINATION"),
        ("REBASE", "REBASE_UNIT"),
        ("PARADROP", "PARADROP"),
        ("AIR_SWEEP", "AIR_SWEEP"),
        ("NUCLEAR_STRIKE", "NUCLEAR_STRIKE"),
    ],
)
def test_preview_commands_require_exact_native_preview(mission, kind):
    name = "mission_preview" if mission else "movement_preview"
    arguments = {"target": "unit1", "destination": {"x": 5, "y": 1}, **({"mission": mission} if mission else {})}
    value: JsonObject = {"available": True, "consequences": ["Native warning"], "confirmation": "Explicit decision"}
    add_decision_commands(name, value, arguments)
    assert value["command"] == {"kind": kind, "target": "unit1", "destination": {"x": 5, "y": 1}}
    needed = required_detail(value["command"])
    assert needed is not None
    assert detail_satisfies(DetailQuery(tool=name, arguments=arguments), needed)
    assert not detail_satisfies(
        DetailQuery(tool=name, arguments={**arguments, "destination": {"x": 5, "y": 2}}), needed
    )
    unavailable = {"available": False}
    add_decision_commands(name, unavailable, arguments)
    assert "command" not in unavailable


def test_only_delivered_sections_invalidate_assumptions_and_satisfy_actions(tmp_path):
    async def run():
        memory, io, host = CivilizationState(tmp_path / "private", "Rome"), ServiceIO(), Host()
        host.view = options_snapshot()
        native = host.tool

        async def disband(name, args):
            result = await native(name, args)
            if name == "player_command" and args.get("kind") == "DISBAND_UNIT":
                host.view["map"]["units"] = []
            return result

        host.tool = disband
        gateway = ProposalGateway(memory, io, tmp_path / "workspace")
        job = await gateway.open("game", "gameplay", "g1", host, {}, 0)
        try:
            await gateway.call(job, "player_turn", {}, "overview")
            detail = await gateway.call(job, "unit_details", {"target": "unit1", "sections": ["movement"]}, "movement")
            key = create(
                memory, plan(assumptions=[{"text": "This path is available", "source": detail["evidence"]}]), job=job.id
            )
            command = {"kind": "DISBAND_UNIT", "target": "unit1", "intent": {"reason": "No longer needed"}}
            rejected = await gateway.call(job, "player_command", command, "uninspected")
            assert rejected["error"] == "DETAIL_REQUIRED" and not host.executed
            assert rejected["recovery"]["reads"][0]["arguments"]["sections"] == ["economy"]
            host.view["map"]["units"][0]["economy"]["disband"]["gold"] = 9
            host.revision = "changed-economy"
            await gateway.call(job, "player_turn", {}, "refresh")
            assert not current(memory, key)["needsReview"]
            host.view["map"]["units"][0]["moves"] = []
            host.revision = "changed-movement"
            await gateway.call(job, "player_turn", {}, "changed")
            assert current(memory, key)["needsReview"]
            assert not job.details_read  # Internal reprojection never counts as a delivered read.
            await gateway.call(job, "unit_details", {"target": "unit1", "sections": ["economy"]}, "economy")
            assert (await gateway.call(job, "player_command", command, "inspected"))["done"]
            missing = await gateway.call(job, "player_command", command, "stale-copy")
            assert missing["error"] == "DETAIL_REQUIRED" and len(host.executed) == 1
        finally:
            await gateway.close(job, True)
            await io.close()

    asyncio.run(run())


@pytest.mark.parametrize(
    "arguments,missing",
    [
        ({"kind": "SET_RESEARCH_QUEUE", "selection": ["Pottery"]}, ["queue"]),
        ({"kind": "FOUND_CITY", "target": "unit1", "confirmAction": True}, []),
        ({"kind": "SET_RESEARCH_QUEUE", "queue": ["Pottery"], "intent": "SECRET-PROSE"}, []),
    ],
)
def test_command_errors_describe_only_the_selected_shape_without_echoing_input(arguments, missing):
    arguments["SECRET-KEY"] = "SECRET-VALUE"
    with pytest.raises(ValidationError) as caught:
        command_arguments(arguments)
    error = command_error(arguments, caught.value)
    assert error["missingFields"] == missing and not error["applied"] and error["recoverable"]
    assert "SECRET" not in json.dumps(error)
    shape = error["expectedArguments"]
    correct = {"kind": "SET_RESEARCH_QUEUE", "queue": ["Pottery"], "intent": {"reason": "Learn agriculture"}}
    if arguments["kind"] == "FOUND_CITY":
        correct = {"kind": "FOUND_CITY", "target": "unit1", "intent": {"reason": "Build a capital"}}
        assert "confirmAction" not in shape["properties"] and "confirmBreakPromise" in shape["properties"]
    assert Draft202012Validator(shape).is_valid(correct)


def test_memory_recovery_explains_note_plan_and_keeps_evidence_requirements(tmp_path):
    async def run():
        memory, io, host = CivilizationState(tmp_path / "private", "Rome"), ServiceIO(), Host()
        gateway = ProposalGateway(memory, io, tmp_path / "workspace")
        job = await gateway.open("chat", "communication", "g1", host, {}, 0)

        async def update(content, call):
            return await gateway.call(job, "memory_update", {"changes": [{"op": "create", "content": content}]}, call)

        try:
            rejected = await update({"type": "note", "text": "Discuss borders", "plan": "SECRET-PROSE"}, "plan")
            assert not rejected["changes"][0]["accepted"]
            assert "RELATED_PLAN_REQUIRED" in rejected["changes"][0]["reason"] and "SECRET" not in json.dumps(rejected)
            bad_shape = await update(
                {"type": "note", "text": "Discuss borders", "sources": ["SECRET-SOURCE"], "SECRET-KEY": True}, "shape"
            )
            assert bad_shape["error"] == "INVALID_ARGUMENT" and not bad_shape["applied"]
            assert "sources" not in bad_shape["contentSchemas"]["note"]["properties"]
            assert "SECRET" not in json.dumps(bad_shape)
            assert (await update({"type": "note", "text": "Discuss borders"}, "fixed"))["changes"][0]["accepted"]
            belief = {
                "type": "belief",
                "claim": "Diplomatic context",
                "uncertainty": "medium",
                "sources": ["observation:communication"],
            }
            assert not (await update(belief, "unread"))["changes"][0]["accepted"]
            await gateway.call(job, "chat_read", {}, "read")
            assert (await update(belief, "supported"))["changes"][0]["accepted"]
            assert not host.executed
        finally:
            await gateway.close(job, True)
            await io.close()

    asyncio.run(run())


def test_copied_purchase_still_forwards_to_native_confirmation_and_legality_checks(tmp_path):
    async def run():
        memory, io, host = CivilizationState(tmp_path / "private", "Rome"), ServiceIO(), Host()
        host.view = options_snapshot()
        gateway = ProposalGateway(memory, io, tmp_path / "workspace")
        native_calls = []
        native = host.tool
        rejection = "INVALID_ARGUMENT"

        async def tool(name, args):
            if name == "player_command":
                native_calls.append(copy.deepcopy(args))
                return {"error": {"code": rejection}}
            return await native(name, args)

        host.tool = tool
        job = await gateway.open("purchase", "gameplay", "g1", host, {}, 0)
        try:
            await gateway.call(job, "player_turn", {}, "turn")
            details = await gateway.call(
                job, "construction_details", {"target": "city1", "names": ["Market"]}, "inspect"
            )
            offered = details["data"]["constructions"][0]["purchase"]
            command = {**offered["command"], "intent": {"reason": "Improve gold income"}}
            assert "confirmAction" not in command
            result = await gateway.call(job, "player_command", command, "unconfirmed")
            assert result == {"error": {"code": "INVALID_ARGUMENT"}}
            assert not native_calls[-1].get("confirmAction")
            for rejection in ("OPTION_UNAVAILABLE", "STALE_REVISION"):
                await gateway.call(job, "player_turn", {}, "turn-" + rejection)
                await gateway.call(
                    job, "construction_details", {"target": "city1", "names": ["Market"]}, "inspect-" + rejection
                )
                confirmed = {**command, "confirmAction": True}
                result = await gateway.call(job, "player_command", confirmed, rejection)
                assert result == {"error": {"code": rejection}}
                assert native_calls[-1]["confirmAction"] is True and native_calls[-1]["revision"] == host.revision
                count = len(native_calls)
                assert await gateway.call(job, "player_command", confirmed, rejection) == result
                assert len(native_calls) == count
        finally:
            await gateway.close(job, True)
            await io.close()

    asyncio.run(run())


def test_selective_measurement_uses_the_same_permitted_state_and_omits_contents():
    from tests.verify_selective_actions_live import measure

    snapshot = options_snapshot()
    snapshot["map"]["units"][0]["displayName"] = "PRIVATE-UNIT-NAME"
    args = {"target": "unit1", "sections": ["movement"]}
    detail = project_detail(snapshot, DetailQuery(tool="unit_details", arguments=args))
    command = detail["data"]["entity"]["moves"][0]["command"]
    calls = [
        ("unit_details", args, detail),
        ("player_command", {**command, "intent": {"reason": "PRIVATE-REASON"}}, {"done": True}),
    ]
    measured = measure(calls, [snapshot])
    assert measured["focusedChars"] < measured["equivalentFullChars"]
    assert measured["commandTemplateAddedChars"] > 0
    assert measured["commandsMatchingDeliveredOffers"] == {"MOVE_UNIT": 1}
    assert "PRIVATE" not in json.dumps(measured)
