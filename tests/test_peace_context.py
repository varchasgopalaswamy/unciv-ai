"""Peace evidence stays factual and bounded as native offers and journals grow."""

import copy

import pytest

from civ_agent.coordination_context import encoded
from civ_agent.peace_context import MAX_PEACE_PROMPT_CHARACTERS, peace_prompt_inputs


def evidence():
    return {
        "position": {
            "turn": 150,
            "epoch": "e1",
            "revision": "r1",
            "phase": "ACTIVE",
            "civilizationProfile": {"name": "Rome"},
            "economy": {"treasury": 200, "availableGold": 150, "goldPerTurn": -3, "happiness": 4},
            "cities": [
                {
                    "handle": "city1",
                    "name": "Rome",
                    "health": 123,
                    "maxHealth": 200,
                    "development": {
                        "population": 5,
                        "position": {"x": 0, "y": 0},
                        "yields": [{"stat": "Production", "amount": "10"}],
                    },
                    "constructionEstimates": [{"building": "CONSTRUCTION_SENTINEL " * 100000}],
                    "purchases": ["PURCHASE_SENTINEL " * 10000],
                }
            ],
            "map": {
                "units": [
                    {
                        "handle": "unit1",
                        "name": "Warrior",
                        "military": True,
                        "health": 42,
                        "position": {"x": 1, "y": 1},
                        "combat": {"strength": 8, "attacks": ["EXECUTABLE_OFFER " * 10000]},
                    }
                ],
                "tiles": [
                    {
                        "visible": True,
                        "position": {"x": 2, "y": 2},
                        "owner": "Greece",
                        "terrain": "Hill",
                        "units": [{"civilization": "Greece", "name": "Pikeman", "health": 38}],
                    },
                    {
                        "visible": False,
                        "position": {"x": 9, "y": 9},
                        "owner": "Greece",
                        "cityName": "Athens",
                        "observedTurn": 120,
                        "units": [{"name": "STALE_UNIT_SENTINEL"}],
                    },
                ],
            },
        },
        "warState": {
            "episodes": {
                "war1": {
                    "id": "war1",
                    "active": True,
                    "opponent": "Greece",
                    "version": 2,
                    "goals": [{"outcome": "Defend the frontier; acceptable costs remain under discussion"}],
                    "history": ["HISTORY_SENTINEL " * 100000],
                    "proposals": {},
                }
            },
            "operations": {
                "defense": {"id": "defense", "status": "active", "plan": "Hold the hill"},
                "old": {"id": "old", "status": "completed", "plan": "OLD_PLAN_SENTINEL " * 10000},
            },
            "operationHistory": {"defense": ["JOURNAL_SENTINEL " * 100000]},
            "messages": [
                {"id": "ack", "to": "military", "acknowledged": True, "text": "READ_MESSAGE_SENTINEL " * 10000}
            ],
        },
        "exactPeaceTerms": {
            "partner": "Greece",
            "give": [{"type": "Treaty", "name": "Peace Treaty", "amount": 1, "duration": 10}],
            "receive": [{"type": "Gold", "name": "Gold", "amount": 17, "duration": -1}],
        },
        "privateMemory": "Current strategic advice; avoid assuming unseen reinforcements.",
    }


def test_native_menus_and_history_do_not_expand_the_frozen_peace_prompt():
    inputs = evidence()
    original = copy.deepcopy(inputs)
    result = peace_prompt_inputs(inputs, "military")
    assert inputs == original
    assert len(encoded(inputs)) > 1000000
    assert len(encoded(result)) < 10000
    assert result["exactPeaceTerms"] == inputs["exactPeaceTerms"]
    assert result["warState"]["episodes"]["war1"]["goals"] == inputs["warState"]["episodes"]["war1"]["goals"]
    for sentinel in (
        "CONSTRUCTION_SENTINEL",
        "PURCHASE_SENTINEL",
        "EXECUTABLE_OFFER",
        "HISTORY_SENTINEL",
        "OLD_PLAN_SENTINEL",
        "JOURNAL_SENTINEL",
        "READ_MESSAGE_SENTINEL",
        "STALE_UNIT_SENTINEL",
    ):
        assert sentinel not in encoded(result)
    assert result["position"]["units"][0]["health"] == 42
    assert result["position"]["visibleUnits"][0]["health"] == 38
    assert result["position"]["rememberedCities"][0]["observedTurn"] == 120
    assert not result["position"]["rememberedCities"][0]["visible"]


def test_large_armies_have_explicit_coverage_and_do_not_displace_exact_terms_or_goals():
    inputs = evidence()
    inputs["position"]["map"]["units"] *= 1000
    inputs["position"]["cities"] *= 100
    inputs["position"]["map"]["tiles"] *= 500
    inputs["militaryOverview"] = {f"assessment{i}": "Exact military assessment " * 160 for i in range(4)}
    result = peace_prompt_inputs(inputs, "gameplay")
    assert len(encoded(result)) <= MAX_PEACE_PROMPT_CHARACTERS
    assert result["coverage"]["totals"]["units"] == 1000
    assert result["coverage"]["omitted"]["units"] > 0
    assert result["exactPeaceTerms"] == inputs["exactPeaceTerms"]
    assert result["militaryOverview"] == inputs["militaryOverview"]
    assert result["warState"]["episodes"]["war1"]["goals"] == inputs["warState"]["episodes"]["war1"]["goals"]


def test_mandatory_authored_terms_are_never_silently_truncated():
    inputs = evidence()
    inputs["exactPeaceTerms"]["give"][0]["name"] = "Exact wording " * 10000
    with pytest.raises(ValueError, match="PEACE_REQUIRED_CONTEXT_TOO_LARGE"):
        peace_prompt_inputs(inputs, "gameplay")
