import asyncio
import copy
import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator
from pydantic import ValidationError

from civ_agent.civilization_state import CivilizationState
from civ_agent.game_commands import COMMANDS, add_combat_commands, command_arguments, command_schema
from civ_agent.memory_history import summary_markdown
from civ_agent.observations import overview
from civ_agent.proposal_gateway import ProposalGateway, tool_result
from civ_agent.service_io import ServiceIO
from civ_agent.startup_context import prepare_startup, startup_prompt
from civ_agent.state_updates import apply_overview_update
from civ_agent.tactical_review import TacticalReview, tactical_review
from tests.test_proposal_gateway import Host
from tests.test_structured_memory import create, current, replacement, setup, strategy


def france(turn=60):
    return json.loads((Path(__file__).parent / "fixtures/france_defense.json").read_text())["turns"][str(turn)]


def reviewed(turn=60):
    snapshot = france(turn)
    add_combat_commands(snapshot)
    snapshot["tacticalReview"] = tactical_review(snapshot).model_dump()
    return snapshot


def test_france_losses_and_missed_bombardment_are_prominent():
    before = reviewed(25)["tacticalReview"]
    assert before["militaryCount"] == 0
    assert any("Warrior" in loss["text"] for loss in before["recentLosses"])
    assert reviewed(35)["tacticalReview"]["cityBombardments"][0]["city"] == "Orleans"
    view = reviewed()["tacticalReview"]
    TacticalReview.model_validate(view)
    assert view["militaryCount"] == 1
    roster = [u for u in overview(reviewed())["units"] if u["military"]]
    assert roster[0]["health"] == 75
    assert roster[0]["attackOptionCount"] > 0
    assert "military" not in view
    worker = next(u for u in view["civiliansNearHostiles"] if u["handle"] == "unit10")
    assert not worker["otherMilitaryOnSameTile"]
    assert any(t["distance"] == 1 and t["position"] == {"x": -6, "y": -1} for t in worker["nearbyHostiles"])
    shot = view["cityBombardments"][0]
    assert (shot["minDamage"], shot["maxDamage"], shot["maxRetaliation"]) == (30, 32, 0)
    assert shot["command"]["target"] == "city2"
    assert shot["inspection"] == {"tool": "city_details", "arguments": {"target": "city2", "sections": ["combat"]}}
    assert "attackFrom" not in shot["command"]
    assert any("captured our Worker" in loss["text"] for loss in view["recentLosses"])


def test_only_visible_hostile_military_is_summarized_and_preview_fields_are_not_commands():
    snapshot = france()
    hidden = copy.deepcopy(snapshot["map"]["tiles"][0])
    hidden["visible"] = False
    hidden["units"] = [{"name": "SECRET", "civilization": "Rome", "health": 100, "military": True, "atWar": True}]
    snapshot["map"]["tiles"].append(hidden)
    add_combat_commands(snapshot)
    review = tactical_review(snapshot)
    assert "SECRET" not in review.model_dump_json()
    assert all(t.name != "Worker" and t.civilization == "Barbarians" for t in review.visibleHostiles)
    for city in snapshot["cities"]:
        for attack in city["attacks"]:
            assert "attackFrom" in attack and "attackFrom" not in attack["command"]
            assert command_arguments(attack["command"]) == attack["command"]
    for unit in snapshot["map"]["units"]:
        for attack in unit["combat"]["attacks"]:
            assert attack["command"]["attackFrom"] == attack["attackFrom"]
        for order in unit["combat"]["orders"]:
            assert ("command" in order) == order["available"]


def captive_tile(snapshot, *, ours=True):
    tile = copy.deepcopy(snapshot["map"]["tiles"][0])
    tile.update(
        position={"x": 7, "y": -1},
        visible=True,
        cityName=None,
        units=[
            {"name": "Brute", "civilization": "Barbarians", "health": 57, "military": True, "atWar": True},
            {
                "name": "Settler",
                "civilization": "Barbarians",
                "health": 100,
                "military": False,
                "atWar": True,
                "capture": {"isOurCapturedUnit": ours, "unitIfKept": "Settler" if ours else "Worker"},
            },
        ],
    )
    snapshot["map"]["tiles"] = [t for t in snapshot["map"]["tiles"] if t["position"] != tile["position"]] + [tile]
    return tile


@pytest.mark.parametrize("ours", [True, False])
def test_potential_captures_are_visible_opportunities_separate_from_threats(ours):
    snapshot = france()
    add_combat_commands(snapshot)
    tile = captive_tile(snapshot, ours=ours)
    hidden = copy.deepcopy(tile)
    hidden["visible"] = False
    hidden["position"] = {"x": 12, "y": 12}
    snapshot["map"]["tiles"].append(hidden)
    view = tactical_review(snapshot)
    assert len(view.potentialCaptureTargets) == 1
    captive = view.potentialCaptureTargets[0]
    assert captive.name == "Settler"
    assert captive.capture.unitIfKept == ("Settler" if ours else "Worker")
    assert captive.capture.isOurCapturedUnit is ours
    assert "originalOwner" not in captive.model_dump_json()
    assert captive.civilization == "Barbarians" and not captive.requiresWarDeclaration
    assert captive.guard is not None and (captive.guard.name, captive.guard.health) == ("Brute", 57)
    assert captive.directCaptureOrders == []
    assert all(threat.name != "Settler" for threat in view.visibleHostiles)
    # A civilian with no visible guard can also belong to a player currently at peace.
    tile["units"] = [tile["units"][1]]
    tile["units"][0].update(civilization="Rome", atWar=False)
    unguarded = tactical_review(snapshot).potentialCaptureTargets[0]
    assert unguarded.requiresWarDeclaration and unguarded.guard is None
    assert unguarded.postDeclarationReachability == "NOT_EVALUATED"
    assert unguarded.directCaptureOrders == []
    # Do not infer original ownership or a recovery result from a unit name/loss report.
    tile["units"][0].pop("capture")
    absent = tactical_review(snapshot).model_dump(exclude_none=True)
    assert absent["potentialCaptureTargets"] == [] and "captureConditions" not in absent


def capture_scenario():
    snapshot = france()
    add_combat_commands(snapshot)
    unit = next(u for u in snapshot["map"]["units"] if u["military"])
    unit.update(handle="unit4", name="Scout", position={"x": -7, "y": 1}, movement="1/2", moves=[])
    unit["combat"].update(attacks=[], role="RECON")
    snapshot["map"]["units"] = [unit]
    tile = captive_tile(snapshot, ours=False)
    tile.update(position={"x": -6, "y": 0}, units=[tile["units"][1]])
    tile["units"][0].update(civilization="France", atWar=False)
    return snapshot, unit, tile


def test_greece_scout_is_two_hexes_away_and_war_does_not_establish_reachability():
    snapshot, unit, tile = capture_scenario()
    target = tactical_review(snapshot).potentialCaptureTargets[0]
    assert target.requiresWarDeclaration and target.postDeclarationReachability == "NOT_EVALUATED"
    assert target.nearestMilitary[0].model_dump() == {"unit": "unit4", "distanceInHexes": 2, "movement": "1/2"}
    assert target.directCaptureOrders == []
    # Native offers after declaring war still only reach the intervening tile.
    tile["units"][0]["atWar"] = True
    unit["moves"] = [{"destination": {"x": -6, "y": 1}}]
    target = tactical_review(snapshot).potentialCaptureTargets[0]
    assert not target.requiresWarDeclaration and target.postDeclarationReachability is None
    assert target.directCaptureOrders == []


@pytest.mark.parametrize("kind", ["MOVE_UNIT", "ATTACK"])
def test_direct_capture_commands_require_native_offers_and_detail_inspection(kind):
    snapshot, unit, tile = capture_scenario()
    tile["units"][0]["atWar"] = True
    if kind == "MOVE_UNIT":
        unit["moves"] = [{"destination": tile["position"]}]
    else:
        unit["combat"]["attacks"] = [
            {
                "destination": tile["position"],
                "attackFrom": unit["position"],
                "consequences": {"civilianOrCityCapturePossible": True},
            }
        ]
    target = tactical_review(snapshot).potentialCaptureTargets[0]
    order = target.directCaptureOrders[0]
    command = order.command.model_dump(exclude_none=True, exclude_defaults=True)
    assert command_arguments(command) == command
    assert command == {
        "kind": kind,
        "target": "unit4",
        "destination": tile["position"],
        **({"attackFrom": unit["position"]} if kind == "ATTACK" else {}),
    }
    assert order.inspection == {
        "tool": "unit_details",
        "arguments": {"target": "unit4", "sections": ["movement" if kind == "MOVE_UNIT" else "combat"]},
    }
    # No peaceful capture is manufactured even if an inconsistent offer were present.
    tile["units"][0]["atWar"] = False
    assert tactical_review(snapshot).potentialCaptureTargets[0].directCaptureOrders == []


def test_ranged_fire_is_not_capture_but_a_ranged_unit_can_capture_by_moving():
    snapshot, unit, tile = capture_scenario()
    tile["units"][0]["atWar"] = True
    unit["combat"].update(
        role="RANGED",
        rangedStrength=6,
        attacks=[
            {
                "destination": tile["position"],
                "attackFrom": unit["position"],
                "consequences": {"civilianOrCityCapturePossible": False},
            }
        ],
    )
    assert tactical_review(snapshot).potentialCaptureTargets[0].directCaptureOrders == []
    unit["moves"] = [{"destination": tile["position"]}]
    assert tactical_review(snapshot).potentialCaptureTargets[0].directCaptureOrders[0].command.kind == "MOVE_UNIT"
    # A city or military guard requires separate combat, not a direct capture order.
    tile["cityName"] = "Paris"
    target = tactical_review(snapshot).potentialCaptureTargets[0]
    assert target.city == "Paris" and target.directCaptureOrders == []
    tile["cityName"] = None
    tile["units"].append({"name": "Warrior", "civilization": "France", "health": 100, "military": True, "atWar": True})
    assert tactical_review(snapshot).potentialCaptureTargets[0].directCaptureOrders == []


def test_capture_distances_wrap_and_do_not_filter_native_routes_by_distance():
    snapshot, unit, tile = capture_scenario()
    tile["position"] = {"x": -4, "y": 0}
    tile["units"][0]["atWar"] = True
    unit.update(position={"x": 4, "y": 0}, movement="0/2")
    snapshot["map"]["wrapOffset"] = {"x": 10, "y": 0}
    target = tactical_review(snapshot).potentialCaptureTargets[0]
    assert target.nearestMilitary[0].distanceInHexes == 2
    assert target.directCaptureOrders == []
    # A farther unit with a native route (e.g. along roads) is not omitted by a distance budget or top-two limit.
    for handle, position in [("near", {"x": -3, "y": 0}), ("far", {"x": 0, "y": 0})]:
        other = copy.deepcopy(unit)
        other.update(handle=handle, position=position)
        snapshot["map"]["units"].append(other)
    other.update(movement="1/2", moves=[{"destination": tile["position"]}])
    target = tactical_review(snapshot).potentialCaptureTargets[0]
    assert [u.unit for u in target.nearestMilitary] == ["near", "unit4"]
    assert [o.command.target for o in target.directCaptureOrders] == ["far"]


@pytest.mark.parametrize("ours", [True, False])
def test_capture_is_preloaded_and_command_updates_clear_it_without_an_extra_overview(tmp_path, ours):
    async def run():
        io = ServiceIO()
        memory = CivilizationState(tmp_path / "private", "France")
        gateway = ProposalGateway(memory, io, tmp_path / "workspace")
        host = Host()
        host.civilization = "France"
        host.view = france()
        tile = captive_tile(host.view, ours=ours)
        original = host.tool

        async def tool(name, args):
            if name in {"player_capabilities", "victory_progress"}:
                return {}
            return await original(name, args)

        host.tool = tool
        try:
            job = await gateway.open("recapture", "gameplay", "g1", host, {}, game_turn=60)
            prepared = await prepare_startup(gateway, job)
            prompt = startup_prompt(prepared, "gameplay", "Play this turn")
            reads = json.JSONDecoder().raw_decode(prompt.user.split("Prepared player-visible reads:\n", 1)[1])[0]
            initial = reads["player_turn"]
            assert initial["tacticalReview"]["potentialCaptureTargets"][0]["capture"] == tile["units"][1]["capture"]
            region = await gateway.call(job, "map_region", {"center": tile["position"], "radius": 0}, "inspect-map")
            assert region["data"]["tiles"][0]["units"][1]["capture"] == tile["units"][1]["capture"]
            # A permitted action refresh no longer contains the captive (captured, dead or out of sight).
            city = await gateway.call(job, "city_details", {"target": "city2", "sections": ["combat"]}, "inspect")
            shot = city["data"]["entity"]["attacks"][0]["command"]
            tile["units"] = []
            result = await gateway.call(
                job, "player_command", {**shot, "intent": {"reason": "Damage the Brute"}}, "act"
            )
            assert "stateUpdate" in result, result
            current = apply_overview_update(initial, result["stateUpdate"])
            assert current["tacticalReview"]["potentialCaptureTargets"] == []
            assert "captureConditions" not in current["tacticalReview"]
            assert any(q["tool"] == "map_region" for q in result["stateUpdate"]["invalidatedDetails"])
        finally:
            await io.close()

    asyncio.run(run())


def test_expansion_and_defender_losses_flag_advisory_plans(tmp_path):
    memory = setup(tmp_path)
    view = reviewed()
    view["tacticalReview"]["nonReconMilitaryCount"] = 2
    memory.observe_player(view, "game")
    defense = create(memory, strategy(subjects=["defense"]))
    view["tacticalReview"]["cityCount"] += 1
    memory.observe_player(view, "game")
    assert any("city count grew" in reason for reason in current(memory, defense)["needsReview"])
    memory.refresh_memory("game", [defense])
    assert memory.patch("game", [replacement(memory, defense, **{"reasons": ["Accept this risk"]})])["changes"][0][
        "accepted"
    ]
    assert not current(memory, defense)["needsReview"]
    view["tacticalReview"]["nonReconMilitaryCount"] = 1
    memory.observe_player(view, "game")
    assert any("non-recon military count fell" in reason for reason in current(memory, defense)["needsReview"])
    assert memory.run_failure() is None


def test_command_schemas_cover_every_host_action_and_reject_cross_action_fields():
    schema = command_schema()
    native = json.loads(Path("src/main/resources/uncivai/schemas/turn-command.json").read_text())
    kinds = COMMANDS.json_schema()["discriminator"]["mapping"]
    assert set(kinds) == set(native["properties"]["kind"]["enum"])
    values = {
        "target": "unit1",
        "selection": "Worker",
        "queue": ["Mining"],
        "targets": ["notice1"],
        "destination": {"x": 0, "y": 1},
        "attackFrom": {"x": 0, "y": 0},
        "worked": False,
        "locked": True,
        "enabled": False,
        "index": 0,
        "toIndex": 1,
        "confirmWar": True,
    }
    for kind, ref in kinds.items():
        required = COMMANDS.json_schema()["$defs"][ref.rsplit("/", 1)[1]]["required"]
        arguments = {"kind": kind, **{k: values[k] for k in required if k != "kind"}}
        if kind == "RESPOND_FRIENDSHIP":
            arguments["selection"] = "Accept"
        if kind == "AUTOPLAY":
            arguments["selection"] = "ECONOMY"
        assert Draft202012Validator(schema).is_valid(arguments), kind
        assert command_arguments(arguments) == arguments
        request = {"requestId": "r1", "epoch": "g1", "revision": "rev1", **arguments}
        assert Draft202012Validator(native).is_valid(request), kind
    for invalid in (
        {"kind": "CITY_ATTACK", "target": "city2", "destination": {"x": -6, "y": -1}, "attackFrom": {"x": -5, "y": -1}},
        {"kind": "ATTACK", "target": "unit1", "destination": {"x": 0, "y": 1}},
        {"kind": "END_TURN", "selection": "Worker"},
        {"kind": "SET_AVOID_GROWTH", "target": "city1", "enabled": 1},
        {"kind": "DECLARE_WAR", "target": "Greece", "confirmWar": 1},
    ):
        with pytest.raises(ValidationError):
            command_arguments(invalid)


def test_gateway_reports_shape_errors_but_tactical_concerns_do_not_require_deferral(tmp_path):
    async def run():
        io = ServiceIO()
        memory = CivilizationState(tmp_path / "private", "France")
        gateway = ProposalGateway(memory, io, tmp_path / "workspace")
        host = Host()
        host.civilization = "France"
        original = host.tool

        async def tool(name, args):
            return france() if name == "player_turn" else await original(name, args)

        host.tool = tool
        try:
            job = await gateway.open("turn60", "gameplay", "g1", host, {}, game_turn=60)
            view = await gateway.call(job, "player_turn", {}, "read")
            assert view["tacticalReview"]["civiliansNearHostiles"]
            assert view["strategyReview"] == {"assessments": []}
            encoded = tool_result(view)["content"][0]["text"]
            assert encoded.index('"tacticalReview"') < encoded.index('"cities"')
            city = await gateway.call(job, "city_details", {"target": "city2"}, "inspect")
            shot = city["data"]["entity"]["attacks"][0]
            result = await gateway.call(
                job,
                "player_command",
                {**shot["command"], "attackFrom": shot["attackFrom"], "intent": {"reason": "Protect the worker"}},
                "wrong",
            )
            assert result["error"] == "INVALID_COMMAND_ARGUMENTS"
            assert "attackFrom" not in result["allowedFields"]
            assert not host.executed and not memory.pending()
            result = await gateway.call(
                job, "player_command", {**shot["command"], "intent": {"reason": "Protect the worker"}}, "shot"
            )
            assert result["done"] and "attackFrom" not in host.executed[0]
            await gateway.call(job, "player_turn", {}, "again")
            result = await gateway.call(
                job,
                "player_command",
                {"kind": "END_TURN"},
                "end-reviewed",
            )
            assert result["done"]
        finally:
            await io.close()

    asyncio.run(run())


def test_losses_flag_global_strategy_without_subject_tags_and_survive_restart(tmp_path):
    memory = setup(tmp_path)
    defense = create(memory, strategy())
    note = create(memory, {"type": "note", "text": "Found a second city when practical"})
    view = reviewed(25)
    memory.observe_player(view, "game")
    assert any("losses" in reason for reason in current(memory, defense)["needsReview"])
    assert not current(memory, note)["needsReview"]
    restarted = CivilizationState(tmp_path / "private", "Rome")
    assert current(restarted, defense)["needsReview"] == current(memory, defense)["needsReview"]
    restarted.start_job("resumed", "gameplay", {}, game_turn=25)
    restarted.observe_player(view, "resumed")
    restarted.refresh_memory("resumed", [defense])
    result = restarted.patch("resumed", [replacement(restarted, defense, assessment="Defense needs reinforcement")])
    assert result["changes"][0]["accepted"]
    assert not current(restarted, defense)["needsReview"]
    restarted.observe("communication", {"context": {"turn": 30}}, "resumed")
    # Advancing the calendar does not manufacture a strategy review.
    assert not current(restarted, defense)["needsReview"]


def test_loss_survives_the_short_summary_word_limit():
    summary = {
        "turn": 60,
        "actions": [{"action": "Started a routine improvement " * 30, "reason": "Economy", "outcome": "confirmed"}]
        * 20,
        "observations": [{"id": "observation:tactics", "body": reviewed()["tacticalReview"]}],
        "memoryChanges": [],
        "unresolved": [],
    }
    result = summary_markdown(summary)
    assert "captured our Worker" in result
    assert len(result.split()) <= 200
