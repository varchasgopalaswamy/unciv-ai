"""Trustworthy endpoint observations for the paired context evaluation."""

import copy
import json

import pytest

from civ_agent.protocol import JsonObject
from tests.report_role_contexts import final_observation_provenance, improvement_changes, observed_outcomes


def checkpoints(path, change=None):
    game = {
        "civilizations": [{"civID": "Rome", "gold": 10, "tech": {"techsResearched": ["Pottery", "Mining"]}}],
        "tileMap": {"tileList": [{"position": {"x": 1}, "exploredBy": ["Rome"]}]},
    }
    loaded = copy.deepcopy(game)
    loaded["civilizations"][0]["tech"]["techsResearched"].reverse()
    loaded["tileMap"]["tileList"][0]["exploredBy"].append("Greece")
    if change:
        change(loaded)
    (path / "final-observation").mkdir()
    for name, value in (("exploration.json", game), ("final-observation/exploration.json", loaded)):
        (path / name).write_text(json.dumps({"gameJson": json.dumps(value)}))


def test_native_reload_bookkeeping_is_disclosed_without_changing_source(tmp_path):
    checkpoints(tmp_path)
    before = (tmp_path / "exploration.json").read_bytes()
    result = final_observation_provenance(tmp_path)
    assert result["nativeStateMatchesExceptReloadBookkeeping"]
    assert result["reloadExplorationChanges"] == [
        {"position": {"x": 1}, "before": ["Rome"], "after": ["Rome", "Greece"]}
    ]
    assert (tmp_path / "exploration.json").read_bytes() == before


def test_changed_gameplay_state_cannot_be_used_as_a_final_observation(tmp_path):
    checkpoints(tmp_path, lambda game: game["civilizations"][0].update(gold=11))
    with pytest.raises(ValueError, match="changed native state"):
        final_observation_provenance(tmp_path)


def endpoint(tmp_path):
    checkpoints(tmp_path)
    (tmp_path / "initial-turn.json").write_text(
        json.dumps(
            {
                "turn": 10,
                "research": {"sciencePerTurn": 20},
                "map": {"units": [{"handle": "unit1", "name": "Warrior", "military": True}]},
            }
        )
    )
    final = {
        "turn": 20,
        "research": {"sciencePerTurn": 30},
        "map": {"units": [{"handle": "unit1", "name": "Warrior", "military": True}]},
        "combatReports": [
            {"id": "report1", "turn": 19, "defender": {"isOwn": True, "name": "Warrior"}, "outcome": "Destroyed"}
        ],
    }
    (tmp_path / "final-observation/initial-turn.json").write_text(json.dumps(final))
    return {
        "commands": [],
        "outcomes": [
            {"populationDelta": 0, "goldDelta": 0, "technologiesAcquired": [], "newCities": [], "startingCities": []}
        ],
    }


def test_endpoint_includes_construction_and_losses_after_last_agent_read(tmp_path):
    result = observed_outcomes(tmp_path, {"sourcePosition": str(tmp_path)}, endpoint(tmp_path))
    assert result["ratesAfter"]["sciencePerTurn"] == 30
    assert result["ratesAtLastAgentObservation"]["sciencePerTurn"] == 20
    assert result["confirmedObservedMilitaryLossReports"] == 1
    provenance = result["finalNativeObservation"]
    assert provenance is not None and provenance["nativeStateMatchesExceptReloadBookkeeping"]


def test_invalid_reload_falls_back_to_dated_agent_rates(tmp_path):
    details = endpoint(tmp_path)
    checkpoint = tmp_path / "final-observation/exploration.json"
    saved = json.loads(checkpoint.read_text())
    game = json.loads(saved["gameJson"])
    game["civilizations"][0]["gold"] = 11
    saved["gameJson"] = json.dumps(game)
    checkpoint.write_text(json.dumps(saved))
    original_path = tmp_path / "exploration.json"
    original = json.loads(original_path.read_text())
    final = json.loads((tmp_path / "final-observation/initial-turn.json").read_text())
    original["players"] = {"Rome": {"combatReports": {"native-report-id": final["combatReports"][0]}}}
    original_path.write_text(json.dumps(original))
    result = observed_outcomes(
        tmp_path, {"sourcePosition": str(tmp_path), "position": {"civilization": "Rome"}}, details
    )
    assert result["ratesAfter"]["sciencePerTurn"] == 20
    assert result["finalNativeObservation"] is None
    assert result["rejectedFinalNativeObservation"] is not None
    assert result["confirmedObservedMilitaryLossReports"] == 1


def test_captured_improvement_is_separate_from_construction_on_retained_land(tmp_path):
    game: JsonObject = {
        "civilizations": [{"civID": "Rome", "cities": [{"tiles": [{"x": 0, "y": 0}]}]}],
        "tileMap": {"tileList": [{"position": {}}, {"position": {"x": 1}, "improvement": "Farm"}]},
    }
    (tmp_path / "initial-game.json").write_text(json.dumps(game))
    game["civilizations"][0]["cities"][0]["tiles"].append({"x": 1, "y": 0})
    game["tileMap"]["tileList"][0]["improvement"] = "Farm"
    (tmp_path / "exploration.json").write_text(json.dumps({"gameJson": json.dumps(game)}))
    result = improvement_changes(tmp_path, "Rome")
    assert result["retainedLandChanges"] == [{"position": {"x": 0, "y": 0}, "before": None, "after": "Farm"}]
    assert result["improvementsOnGainedLand"] == {"Farm": 1}
