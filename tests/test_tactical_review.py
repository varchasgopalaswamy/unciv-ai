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
from civ_agent.proposal_gateway import ProposalGateway, tool_result
from civ_agent.service_io import ServiceIO
from civ_agent.tactical_review import TacticalReview, tactical_review
from tests.test_proposal_gateway import Host
from tests.test_structured_memory import create, current, plan, setup


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
    assert view["military"][0]["health"] == 75
    assert view["military"][0]["availableAttacks"] > 0
    worker = next(u for u in view["civiliansNearHostiles"] if u["handle"] == "unit10")
    assert not worker["militaryOnSameTile"]
    assert any(t["distance"] == 1 and t["position"] == {"x": -6, "y": -1} for t in worker["nearbyHostiles"])
    shot = view["cityBombardments"][0]
    assert (shot["minDamage"], shot["maxDamage"], shot["maxRetaliation"]) == (30, 32, 0)
    assert shot["command"]["target"] == "city2"
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
        required = schema["$defs"][ref.rsplit("/", 1)[1]]["required"]
        arguments = {"kind": kind, **{k: values[k] for k in required if k != "kind"}}
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
        assert not Draft202012Validator(schema).is_valid(invalid)
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
            assert view["strategyReview"] == {"plans": []}
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


def test_losses_invalidate_relevant_plans_and_scheduled_reviews_survive_restart(tmp_path):
    memory = setup(tmp_path)
    defense = create(memory, plan(subjects=["defense"]))
    economy = create(memory, plan(objective="Protect workers", subjects=["economy"]))
    expansion = create(memory, plan(objective="Found a second city", subjects=["expansion"], reviewAfterTurn=70))
    assert current(memory, defense).get("reviewAfterTurn") is None
    # The current observation, not the model's prose, establishes the loss.
    view = reviewed(25)
    memory.observe_player(view, "game")
    assert any("losses" in reason for reason in current(memory, defense)["needsReview"])
    assert any("losses" in reason for reason in current(memory, economy)["needsReview"])
    assert not current(memory, expansion)["needsReview"]
    restarted = CivilizationState(tmp_path / "private", "Rome")
    assert current(restarted, defense)["needsReview"] == current(memory, defense)["needsReview"]
    restarted.start_job("resumed", "gameplay", {}, game_turn=25)
    restarted.observe_player(view, "resumed")
    restarted.refresh_memory("resumed", [defense])
    result = restarted.patch(
        "resumed", [{"op": "revise", "id": defense, "content": plan(subjects=["defense"], reviewAfterTurn=30)}]
    )
    assert result["changes"][0]["accepted"]
    assert not current(restarted, defense)["needsReview"]
    restarted.observe("communication", {"context": {"turn": 30}}, "resumed")
    assert current(restarted, defense)["needsReview"] == [
        "Scheduled progress review: check success criteria, then revise or retire this plan"
    ]
    before = restarted.snapshot("gameplay")["records"][defense]["version"]
    restarted.observe("communication", {"context": {"turn": 30}}, "resumed")
    assert restarted.snapshot("gameplay")["records"][defense]["version"] == before


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
