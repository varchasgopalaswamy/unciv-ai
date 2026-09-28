"""Filtered frozen reads preserve native visibility, column order and page semantics."""

import asyncio
import copy
import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from civ_agent.protocol import JsonObject
from civ_agent.strategic_review import frozen_read
from civ_agent.victory_snapshot import SECTIONS, capture_victory, victory_read


def position(turn=5, names=("Scientific", "Domination")) -> JsonObject:
    return {
        "turn": turn,
        "score": 33,
        "maxTurns": 330,
        "sections": list(SECTIONS),
        "availability": {s: {"available": True} for s in SECTIONS},
        "victories": [{"name": name, "milestones": []} for name in names],
        "rivals": [{"civilization": "Greece", "defeated": False, "nextMilestones": {n: "Current" for n in names}}],
        "metrics": [{"name": "Score", "meaning": "Native score"}, {"name": "Production", "meaning": "Production"}],
        "rankings": {
            "comparedCivilizations": 2,
            "scope": "Disclosed only",
            "rows": [
                {
                    "civilization": "Rome",
                    "defeated": False,
                    "values": [33, 7],
                    "ranks": [2, 1],
                    "trend": {"fromTurn": 0, "toTurn": 4, "changes": [4, 2]},
                },
                {"civilization": "Greece", "defeated": False, "values": [40, 5], "ranks": [1, 2], "trend": None},
            ],
        },
        "charts": {
            "turns": list(range(turn + 1)),
            "currentTurn": turn,
            "nextFromTurn": None,
            "requestedToTurn": turn,
            "series": [
                {
                    "civilization": "Rome",
                    "history": [[t + 20, t] if t < turn else [None, None] for t in range(turn + 1)],
                    "currentValues": [33, 7],
                },
                {
                    "civilization": "Greece",
                    "history": [[t + 30, t + 2] if t < turn else [None, None] for t in range(turn + 1)],
                    "currentValues": [40, 5],
                },
            ],
        },
        "demographics": {
            "scope": "Native aggregates",
            "rows": [
                {
                    "metric": "Score",
                    "ownValue": 33,
                    "ownRank": 2,
                    "best": {"civilization": None, "value": None},
                    "average": 38,
                },
                {
                    "metric": "Production",
                    "ownValue": 7,
                    "ownRank": 1,
                    "best": {"civilization": "Rome", "value": 7},
                    "average": 4,
                },
            ],
        },
    }


def test_live_failure_arguments_read_frozen_charts_without_mutating_the_snapshot():
    source = position()
    original = copy.deepcopy(source)
    inputs = {"civilization": "Rome", "reads": {"victory_progress": source}}
    result = frozen_read(
        inputs,
        "victory_progress",
        {
            "sections": ["charts", "rankings", "our_status", "global_status"],
            "metrics": ["Production", "Score"],
            "civilizations": ["Rome"],
            "fromTurn": 1,
            "toTurn": 3,
            "names": ["Scientific"],
        },
    )
    assert [m["name"] for m in result["metrics"]] == ["Score", "Production"]
    assert result["charts"]["turns"] == [1, 2, 3]
    assert result["charts"]["series"] == [
        {"civilization": "Rome", "history": [[21, 1], [22, 2], [23, 3]], "currentValues": None}
    ]
    assert result["charts"]["currentTurn"] is None
    assert result["rankings"]["comparedCivilizations"] == 2
    assert result["rankings"]["rows"][0]["ranks"] == [2, 1]  # Filtering never recalculates ranks.
    assert result["rivals"] == [] and len(result["victories"]) == 1
    assert result["demographics"] is None and source == original


def test_metric_columns_and_demographics_remain_aligned_and_concealed():
    result = victory_read(
        position(),
        {"sections": ["rankings", "charts", "demographics"], "metrics": ["Production"], "civilizations": ["Rome"]},
    )
    row = result["rankings"]["rows"][0]
    assert row["values"] == [7] and row["ranks"] == [1] and row["trend"]["changes"] == [2]
    assert result["charts"]["series"][0]["currentValues"] == [7]
    assert result["charts"]["series"][0]["history"][-1] == [None]
    assert [r["metric"] for r in result["demographics"]["rows"]] == ["Production"]
    hidden = victory_read(position(), {"sections": ["demographics"], "civilizations": ["Rome"]})
    assert hidden["demographics"]["rows"][0]["best"] == {"civilization": None, "value": None}


@pytest.mark.parametrize(
    "args",
    [
        {"sections": []},
        {"sections": ["charts", "charts"]},
        {"sections": ["bad"]},
        {"names": ["Unknown"]},
        {"civilizations": ["Undiscovered"]},
        {"metrics": ["TilesExplored"]},
        {"metrics": ["Score", "Score"]},
        {"fromTurn": 1},
        {"sections": ["charts"], "fromTurn": 3, "toTurn": 2},
        {"sections": ["charts"], "toTurn": 6},
        {"sections": ["charts"], "fromTurn": True},
        {"extra": True},
    ],
)
def test_invalid_or_future_queries_are_rejected(args):
    with pytest.raises(ValueError, match="INVALID_ARGUMENT"):
        victory_read(position(), args)


def test_unavailable_and_uncaptured_sections_never_invent_data():
    source = position()
    source["availability"]["charts"] = {"available": False, "reason": "STATISTICS_DISABLED"}
    source["charts"] = None
    result = victory_read(source, {"sections": ["charts"], "fromTurn": 0})
    assert result["charts"] is None and result["metrics"] == []
    assert result["availability"]["charts"]["reason"] == "STATISTICS_DISABLED"
    source["sections"].remove("demographics")
    result = victory_read(source, {"sections": ["demographics"]})
    assert result["demographics"] is None and result["availability"]["demographics"]["reason"] == "NOT_CAPTURED"


def test_capture_follows_native_pages_and_frozen_queries_can_page_arbitrary_ranges():
    source, calls = position(125), []

    async def read(name, args):
        assert name == "victory_progress"
        calls.append(args)
        return victory_read(source, args)

    captured = asyncio.run(capture_victory(read, 125))
    assert [c["fromTurn"] for c in calls] == [0, 50, 100]
    assert captured["charts"] == source["charts"]
    default = victory_read(captured, {})
    assert default["sections"] == ["our_status", "global_status", "rankings"] and default["charts"] is None
    charts = victory_read(captured, {"sections": ["charts"]})["charts"]
    assert charts["turns"] == list(range(106, 126))
    page = victory_read(captured, {"sections": ["charts"], "fromTurn": 3, "toTurn": 120})["charts"]
    assert page["turns"] == list(range(3, 53)) and page["nextFromTurn"] == 53 and page["requestedToTurn"] == 120


@pytest.mark.parametrize("changed_page", [0, 1])
def test_capture_rejects_a_position_change(changed_page):
    calls = 0

    async def read(name, args):
        nonlocal calls
        result = victory_read(position(125), args)
        if calls == changed_page:
            result["turn"] += 1
        calls += 1
        return result

    with pytest.raises(ValueError, match="STRATEGIC_REVIEW_POSITION_CHANGED"):
        asyncio.run(capture_victory(read, 125))


def test_victory_query_matches_advertised_native_arguments():
    from civ_agent.victory_snapshot import VictoryQuery

    schema = json.loads(Path("src/main/resources/uncivai/schemas/victory-progress.json").read_text())
    assert set(schema["properties"]) == set(VictoryQuery.model_fields)
    query = {
        "sections": ["charts", "demographics"],
        "metrics": ["Score"],
        "names": ["Scientific"],
        "civilizations": ["Greece"],
        "fromTurn": 0,
        "toTurn": 5,
    }
    Draft202012Validator(schema).validate(query)
    assert victory_read(position(), query)["charts"]["series"][0]["civilization"] == "Greece"
