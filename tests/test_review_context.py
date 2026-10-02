"""Review history and dates stay factual, bounded, frozen and recoverable."""

import copy
import json

import pytest

from civ_agent.civilization_state import CivilizationState
from civ_agent.protocol import JsonObject
from civ_agent.review_context import MAX_CHANGES, MAX_EVENTS, development_changes, review_position, upcoming_events
from civ_agent.review_store import ReviewStore
from tests.test_strategic_review import assessment
from tests.test_structured_memory import setup


def position(turn=5) -> JsonObject:
    return {
        "turn": turn,
        "strategyReview": {"assessment": "OLD_SCIENCE_ONLY_PLAN"},
        "economy": {"goldPerTurn": -2, "happiness": 2},
        "culture": {"culturePerTurn": 4},
        "research": {
            "sciencePerTurn": "20",
            "current": "Iron Working",
            "technologies": [
                {"name": "Mining", "researched": True, "turns": "0"},
                {
                    "name": "Iron Working",
                    "researched": False,
                    "turns": "3",
                    "unlocks": {"units": ["Swordsman"], "buildings": []},
                },
                {"name": "Education", "researched": False, "turns": "2"},
            ],
        },
        "cities": [
            {
                "handle": "city1",
                "name": "Rome",
                "development": {
                    "population": 3,
                    "builtBuildings": ["Palace"],
                    "yields": [{"stat": "Production", "amount": "10"}],
                    "growing": True,
                    "turnsToGrowth": 4,
                    "constructions": [
                        {"index": 0, "name": "Colosseum", "perpetual": False, "turns": 5},
                        {"index": 1, "name": "Library", "perpetual": False, "turns": 1},
                    ],
                },
                "constructionEstimates": {
                    "Colosseum": {
                        "completion": {
                            "yields": {"change": {"Happiness": "2", "Science": "0"}},
                            "maintenanceChange": "1",
                        }
                    },
                },
            },
        ],
        "map": {
            "units": [
                {"handle": "unit1", "name": "Warrior", "military": True},
                {"handle": "unit2", "name": "Archer", "military": True},
                {"handle": "unit3", "name": "Worker", "military": False},
            ],
            "tiles": [{"owner": "Egypt", "cityPopulation": 50}],
        },
        "diplomacy": {
            "knownCivilizations": [{"civilizationName": "Egypt", "peaceTreatyTurns": 6}],
            "cityStates": [
                {
                    "name": "Geneva",
                    "relationship": "Ally",
                    "turnsToRelationshipChange": 2,
                    "influence": 62,
                    "decayPerTurn": 1,
                }
            ],
        },
    }


def test_review_changes_compare_own_output_and_rosters_without_guessing_causes():
    initial = position()
    before = review_position(initial)
    current = copy.deepcopy(initial)
    current["turn"] = 10
    current["economy"]["goldPerTurn"] = 5
    current["research"]["sciencePerTurn"] = "31"
    current["research"]["technologies"][1]["researched"] = True
    current["cities"][0]["development"].update(population=4, builtBuildings=["Palace", "Colosseum"])
    current["cities"].append(
        {
            "handle": "city2",
            "name": "Captured city",
            "development": {
                "population": 2,
                "builtBuildings": ["Library"],
                "yields": [{"stat": "Production", "amount": "3"}],
            },
        }
    )
    current["map"]["units"] = [
        {"handle": "unit1", "name": "Swordsman", "military": True},
        {"handle": "unit4", "name": "Archer", "military": True},
        {"handle": "unit5", "name": "Archer", "military": True},
    ]
    after = review_position(current)
    result = development_changes(before, after)
    assert result["fromTurn"] == 5 and result["toTurn"] == 10
    assert result["totals"]["sciencePerTurn"] == {"before": 20, "now": 31, "change": 11}
    assert result["totals"]["netGoldPerTurn"] == {"before": -2, "now": 5, "change": 7}
    assert result["totals"]["productionPerTurn"]["change"] == 3
    assert result["totals"]["population"]["change"] == 3
    assert result["militaryAdded"] == [{"name": "Archer", "count": 2}]
    assert result["militaryNoLongerOwned"] == [{"name": "Archer", "count": 1}]
    assert result["militaryTypeChanges"] == [{"unit": "unit1", "before": "Warrior", "now": "Swordsman"}]
    assert result["buildingsAdded"] == [{"city": "city1", "name": "Rome", "buildings": ["Colosseum"]}]
    assert result["technologiesAcquired"] == ["Iron Working"]
    assert result["citiesAdded"] == [{"city": "city2", "name": "Captured city"}]
    assert "OLD_SCIENCE_ONLY_PLAN" not in json.dumps(after)
    assert "Egypt" not in json.dumps(result) and "score" not in json.dumps(result).lower()
    assert before == review_position(initial)
    current["cities"][0]["development"]["builtBuildings"].clear()
    assert after["cities"]["city1"]["buildings"] == ["Palace", "Colosseum"]


def test_missing_previous_position_and_missing_rates_are_not_zeroes():
    current = review_position({"turn": 0, "cities": [{"handle": "c", "name": "Rome"}]})
    assert development_changes(None, current) == {"fromTurn": None, "toTurn": 0, "available": False}
    assert current["totals"]["productionPerTurn"] is None and current["totals"]["sciencePerTurn"] is None
    assert "productionPerTurn" not in development_changes(current, review_position(position()))["totals"]
    with pytest.raises(ValueError, match="BASELINE_NOT_EARLIER"):
        development_changes(current, current)


def test_large_development_changes_report_omissions():
    current = position()
    before = review_position(current)
    current["turn"] = 10
    current["cities"][0]["development"]["builtBuildings"] += [f"Building {i}" for i in range(40)]
    current["map"]["units"] += [{"handle": f"new{i}", "name": f"Unit {i}", "military": True} for i in range(40)]
    result = development_changes(before, review_position(current))
    assert len(result["militaryAdded"]) == MAX_CHANGES
    assert result["omittedChanges"]["militaryAdded"] == 40 - MAX_CHANGES
    assert len(result["buildingsAdded"][0]["buildings"]) == MAX_CHANGES
    assert result["buildingsAdded"][0]["omittedBuildings"] == 40 - MAX_CHANGES


def test_timeline_uses_current_work_and_term_expiries_without_adding_scenarios():
    current = position()
    social = {
        "agreements": [
            {
                "agreement": "a1",
                "partner": "Egypt",
                "status": "ACTIVE",
                "remainingTurns": 30,
                "give": [
                    {"type": "Gold", "amount": 200, "duration": 0},
                    {"type": "Treaty", "name": "Peace Treaty", "amount": 1, "duration": 6},
                ],
                "receive": [
                    {"type": "Luxury_Resource", "name": "Spices", "amount": 1, "duration": 2},
                    {"type": "Gold_Per_Turn", "name": "Gold per turn", "amount": 3, "duration": 30},
                ],
            }
        ]
    }
    original = copy.deepcopy((current, social))
    result = upcoming_events(current, social)
    events = result["events"]
    assert [e["inTurns"] for e in events] == [2, 2, 3, 4, 5, 6]
    assert next(e for e in events if e["kind"] == "RESEARCH_COMPLETES")["unlocks"] == {"units": ["Swordsman"]}
    assert next(e for e in events if e["kind"] == "CITY_GROWTH")["populationAfterGrowth"] == 4
    construction = next(e for e in events if e["kind"] == "CONSTRUCTION_COMPLETES")
    assert construction["name"] == "Colosseum" and construction["yieldChange"] == {"Happiness": "2"}
    assert result["happinessNow"] == 2
    text = json.dumps(result)
    assert "Education" not in text and "Library" not in text and "200" not in text
    assert "PEACE_TREATY_ENDS" not in text  # The existing treaty term is the one source.
    assert (current, social) == original
    assert events[0]["receive"] == [{"type": "Luxury_Resource", "name": "Spices", "amount": 1}]


@pytest.mark.parametrize("state", ["wonder", "resistance", "blocked", "perpetual"])
def test_timeline_does_not_schedule_wonders_or_suspended_construction(state):
    current = position()
    city = current["cities"][0]
    if state == "wonder":
        current["wonderOpportunities"] = [{"name": "Colosseum"}]
    elif state == "resistance":
        city["development"]["inResistance"] = True
    elif state == "blocked":
        city["constructionEstimates"]["Colosseum"]["queueUnavailableReasons"] = ["Missing resource"]
    else:
        city["development"]["constructions"][0]["perpetual"] = True
    assert not any(e["kind"] == "CONSTRUCTION_COMPLETES" for e in upcoming_events(current, {})["events"])


def test_timeline_bounds_entries_and_never_invents_dates():
    current = position()
    current["research"]["technologies"][1]["turns"] = "∞"
    current["cities"][0]["development"]["constructions"][0]["turns"] = None
    current["diplomacy"]["cityStates"][0]["turnsToRelationshipChange"] = -1
    current["cities"] = [{**copy.deepcopy(current["cities"][0]), "handle": f"city{i}"} for i in range(30)]
    events = upcoming_events(current, {})
    assert len(events["events"]) == MAX_EVENTS and events["omittedEvents"] == 15
    assert all(e["kind"] == "CITY_GROWTH" for e in events["events"])
    current["cities"].clear()
    assert [e["kind"] for e in upcoming_events(current, {})["events"]] == ["PEACE_TREATY_ENDS"]


def test_avoid_growth_and_stable_city_state_relationships_are_not_deadlines():
    current = position()
    current["cities"][0]["development"]["avoidGrowth"] = True
    current["diplomacy"]["cityStates"][0]["turnsToRelationshipChange"] = 0
    kinds = {e["kind"] for e in upcoming_events(current, {})["events"]}
    assert "CITY_GROWTH" not in kinds and "CITY_STATE_RELATIONSHIP_CHANGE" not in kinds


def test_baseline_publication_is_atomic_with_report_and_rewinds(tmp_path):
    memory = setup(tmp_path)
    store = ReviewStore(memory)
    initial = review_position(position(0))
    store.prepare("opening", {"turn": 0, "observation": position(0)}, {}, 60)
    store.complete("opening", assessment())
    checkpoint = tmp_path / "before-review.json"
    checksum = memory.backup(checkpoint)
    newer = review_position(position(5))
    store.prepare("game", {"turn": 5, "observation": position(5)}, {}, 60)
    assert store.last_position() == initial
    store.failed("game", "Retry")
    assert store.last_position() == initial
    store.complete("game", assessment())
    assert store.last_position() == newer
    store.complete("opening", assessment())  # Late duplicate must not restore an older baseline.
    assert store.last_position() == newer
    memory.engine.dispose()
    reopened = CivilizationState(memory.root, "Rome")
    assert ReviewStore(reopened).last_position() == newer
    reopened.restore(checkpoint, checksum, "restored")
    assert ReviewStore(reopened).last_position() == initial
    reopened.engine.dispose()
