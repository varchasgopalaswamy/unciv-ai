import asyncio
import copy
import json

import pytest
from jsonschema import Draft202012Validator

from civ_agent.game_commands import COMMANDS, command_schema
from civ_agent.observations import TurnOverview, overview
from civ_agent.state_updates import apply_overview_update, overview_update, stamp
from civ_agent.tactical_review import tactical_review
from civ_agent.turn_changes import turn_basis, turn_changes
from tests.test_state_updates import MOVE, session
from tests.test_state_updates import inspect as inspect_unit
from tests.test_structured_memory import create, current, setup, strategy
from tests.test_tactical_review import reviewed


def test_roster_omits_inapplicable_fields_and_delta_clears_old_capabilities():
    snapshot = reviewed()
    worker = snapshot["map"]["units"][0]
    worker.update(worker={"currentJob": "Farm", "remainingTurns": 2}, canFoundCity=False, settlement=None)
    worker["combat"] = {"attacksRemaining": 1, "order": {"automated": True}}
    city = snapshot["cities"][0]
    city.update(health=150, maxHealth=200, defensiveStrength=23)
    initial = overview(snapshot)
    Draft202012Validator(TurnOverview.model_json_schema()).validate(initial)
    unit = initial["units"][0]
    assert unit["workerJobTurnsRemaining"] == 2 and unit["automated"] is True
    assert not {"missions", "carriedUnits", "canFoundCity", "canAssembleSpaceshipPart"} & unit.keys()
    assert initial["cities"][0]["defensiveStrength"] == 23
    worker["worker"] = None
    worker["combat"]["order"]["automated"] = False
    before, after = stamp(initial, "job", "g1", "1"), stamp(overview(snapshot), "job", "g1", "2")
    assert apply_overview_update(before, overview_update(before, after, [])) == after
    assert not {"workerJob", "workerJobTurnsRemaining", "automated"} & after["units"][0].keys()
    assert "attackOptionCount" in after["units"][0] and "movement" in after["units"][0]


def test_healthy_military_near_visible_hostiles_is_still_in_tactical_review():
    snapshot = reviewed()
    soldier = next(unit for unit in snapshot["map"]["units"] if unit["military"] and unit["nearbyHostiles"])
    soldier["health"] = 100
    assert soldier["handle"] in {unit.handle for unit in tactical_review(snapshot).militaryNearHostiles}


def test_compact_command_envelope_preserves_all_action_kinds_and_is_smaller():
    full, compact = COMMANDS.json_schema(), command_schema()
    assert set(compact["properties"]["kind"]["enum"]) == set(full["discriminator"]["mapping"])
    assert len(json.dumps(compact)) < len(json.dumps(full)) / 2
    Draft202012Validator.check_schema(compact)


def test_old_irrelevant_notes_are_searchable_but_not_used_to_fill_preload(tmp_path):
    memory = setup(tmp_path)
    old = create(memory, {"type": "note", "text": "Incense deal paid 100 gold", "subjects": ["Egypt"]})
    memory.finish_job("game", True)
    memory.start_job("next", "gameplay", {}, game_turn=40)
    recent = create(memory, {"type": "note", "text": "Greece may want peace", "subjects": ["Greece"]}, "next")
    selected = memory.snapshot("gameplay", game_turn=40)
    assert recent in selected["contextIds"] and old not in selected["contextIds"]
    assert selected["omitted"] == 1
    assert memory.search_memory("next", {"query": "Incense"})["records"][0]["id"] == old
    memory.ingest("g1", [{"id": 1, "turn": 40, "kind": "message", "body": {"author": "Egypt"}}], 1)
    assert old in memory.snapshot("gameplay", game_turn=40)["contextIds"]
    assert old not in memory.snapshot("gameplay", game_turn=43)["contextIds"]


def test_routine_changes_do_not_raise_notices_but_resource_loss_and_milestones_do(tmp_path):
    memory = setup(tmp_path)
    view = {"turn": 4, "economy": {"happiness": 4, "treasury": 10, "resources": [{"name": "Iron", "available": 2}]}}
    memory.observe_player(view, "game", {"turn": 4, "researchQueue": ["Writing"]})
    key = create(memory, strategy(assumptions=[{"text": "Iron remains available", "source": "observation:overview"}]))
    view["turn"] = 5
    view["economy"]["treasury"] = 40
    memory.observe_player(view, "game", {"turn": 5, "researchQueue": ["Philosophy"]})
    assert not current(memory, key)["needsReview"]
    view["economy"]["resources"][0]["available"] = 0
    memory.observe_player(view, "game", {"turn": 5})
    assert "Resources no longer available: Iron" in current(memory, key)["needsReview"]
    victory = {
        "turn": 5,
        "score": 100,
        "victories": [{"name": "Science", "milestones": [{"description": "Apollo", "status": "Current"}]}],
    }
    memory.observe("victory_progress", victory, "game")
    victory["score"] = 101
    memory.observe("victory_progress", victory, "game")
    assert not any("Victory" in reason for reason in current(memory, key)["needsReview"])
    victory["victories"][0]["milestones"][0]["status"] = "Completed"
    memory.observe("victory_progress", victory, "game")
    assert any("Victory" in reason for reason in current(memory, key)["needsReview"])


def test_turn_changes_reference_existing_notices_and_distinguish_known_completion():
    snapshot = reviewed()
    snapshot["turn"] = 10
    worker, moving = snapshot["map"]["units"][:2]
    worker["worker"] = {"currentJob": "Farm"}
    moving["movementOrder"] = {"destination": {"x": 80, "y": 80}}
    baseline = turn_basis(snapshot)
    snapshot["turn"] = 11
    worker["worker"] = {"currentJob": None}
    moving["movementOrder"] = None
    snapshot["map"]["tiles"].append({"position": worker["position"], "visible": True, "improvement": "Farm"})
    city = snapshot["cities"][0]
    city.setdefault("constructionOptions", []).append("University")
    snapshot["notifications"] = [{"handle": "notice-new", "text": "Farm complete"}]
    snapshot["economy"]["goldenAge"] = {"turnsRemaining": 2}
    snapshot["communication"] = {"agreements": [{"agreement": "a1", "remainingTurns": 1, "status": "ACTIVE"}]}
    changes = turn_changes(baseline, snapshot)
    assert changes["notifications"] == ["notice-new"] and "Farm complete" not in json.dumps(changes)
    assert changes["workerJobs"][0]["outcome"] == "COMPLETED"
    assert changes["orders"][0]["outcome"] == "INTERRUPTED"
    assert changes["newConstructionOptions"] == [{"city": city["handle"], "names": ["University"]}]
    assert changes["agreementsExpiring"] == ["a1"] and changes["goldenAgeEndingInTurns"] == 2
    snapshot["map"]["tiles"][-1]["visible"] = False
    assert turn_changes(baseline, snapshot)["workerJobs"][0]["outcome"] == "ENDED"


@pytest.mark.parametrize(
    "visible,pillaged,outcome", [(True, False, "COMPLETED"), (False, False, "ENDED"), (True, True, "ENDED")]
)
def test_worker_starting_same_job_elsewhere_reports_previous_site(visible, pillaged, outcome):
    snapshot = reviewed()
    worker = snapshot["map"]["units"][0]
    worker["worker"] = {"currentJob": "Farm"}
    baseline = turn_basis(snapshot)
    original = copy.deepcopy(baseline)
    assert "workerJobs" not in turn_changes(baseline, snapshot)
    old_position = copy.deepcopy(worker["position"])
    worker["position"] = {"x": old_position["x"] + 1, "y": old_position["y"]}
    snapshot["map"]["tiles"].append(
        {"position": old_position, "visible": visible, "improvement": "Farm", "improvementPillaged": pillaged}
    )
    assert turn_changes(baseline, snapshot)["workerJobs"] == [
        {"unit": worker["handle"], "job": "Farm", "position": old_position, "outcome": outcome}
    ]
    assert baseline == original


def test_removed_units_and_cities_keep_only_last_owned_identity_and_location():
    snapshot = reviewed()
    unit, city = snapshot["map"]["units"][0], snapshot["cities"][0]
    city.setdefault("development", {})["position"] = {"x": 7, "y": 8}
    baseline = turn_basis(snapshot)
    snapshot["map"]["units"].remove(unit)
    snapshot["cities"].remove(city)
    # A later sighting must not replace the location known while we owned it.
    snapshot["map"]["tiles"].append(
        {"position": {"x": 99, "y": 98}, "visible": True, "units": [{"name": unit["name"], "civilization": "Egypt"}]}
    )
    changes = turn_changes(baseline, snapshot)
    for key, entity, position in (
        ("unitsNoLongerOwned", unit, unit["position"]),
        ("citiesNoLongerOwned", city, city["development"]["position"]),
    ):
        assert changes[key] == [{"handle": entity["handle"], "name": entity["name"], "lastKnownPosition": position}]
    assert "Egypt" not in json.dumps(changes)


def test_opening_changes_survive_refresh_and_do_not_treat_own_moves_as_interruptions(tmp_path):
    async def run():
        async with session(tmp_path) as (gateway, job, host, _):
            await gateway.call(job, "player_turn", {}, "start")
            host.view["turn"] += 1
            host.view["cities"][0]["constructionOptions"].append("University")
            host.revision = "next-turn"
            opening = await gateway.call(job, "player_turn", {}, "opening")
            changes = copy.deepcopy(opening["changesSincePreviousTurn"])
            assert changes["newConstructionOptions"][0]["names"] == ["University"]
            await inspect_unit(gateway, job)
            await gateway.call(job, "player_command", MOVE, "move")
            assert job.delivered_overview is not None
            assert job.delivered_overview["changesSincePreviousTurn"] == changes

    asyncio.run(run())
