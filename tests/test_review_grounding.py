"""Regressions from the benchmark's population/city-count and planning failures."""

import json

import pytest

from civ_agent.memory_records import public_record
from civ_agent.memory_store import RecordStore
from civ_agent.observations import overview
from civ_agent.review_grounding import MaterialAssumption, named_facts
from civ_agent.strategic_review import parse_advice, review_named_facts
from tests.test_strategic_review import assessment


def position():
    return {
        "turn": 20,
        "phase": "ACTIVE",
        "canEndTurn": True,
        "cities": [
            {
                "handle": "ours",
                "name": "Paris",
                "constructionQueue": [],
                "puppet": False,
                "attacksRemaining": 0,
                "development": {"population": 3},
            }
        ],
        "map": {
            "tiles": [
                {"position": {"x": 1, "y": 2}, "owner": "Greece", "cityName": "Athens", "visible": True},
                {"position": {"x": 3, "y": 4}, "owner": "Greece", "cityName": "Sparta", "visible": False},
            ]
        },
    }


def facts():
    return named_facts(
        position(),
        {
            "metrics": [{"name": "Population", "definition": "Sum of city population"}],
            "rankings": {"rows": [{"civilization": "Greece", "values": [3]}]},
        },
        civilization="France",
    )


def test_population_never_establishes_foreign_city_count_or_current_stale_sightings():
    values = {fact["id"]: fact for fact in facts()}
    assert values["rival/Greece/totalPopulation"]["value"] == 3
    assert values["rival/Greece/empireCityCount"]["value"] is None
    assert values["rival/Greece/visibleCityCountLowerBound"]["value"] == 1
    assert values["rival/Greece/observedCityLocationsCount"]["value"] == 2
    assert values["our/empireCityCount"]["value"] == 1
    assert not any(fact["subject"] == "Sparta" for fact in facts())


@pytest.mark.parametrize(
    "reference",
    [
        {"id": "rival/Greece/empireCityCount", "value": 3},
        {"id": "rival/Greece/hiddenCities", "value": 3},
        {"id": "rival/Greece/totalPopulation", "value": True},
    ],
)
def test_review_rejects_invented_or_misquoted_facts(reference):
    value = assessment()
    value["review"]["materialAssumptions"] = [
        {"assessment": "Greece's expansion changes our opportunity window.", "status": "inferred", "facts": [reference]}
    ]
    with pytest.raises(ValueError, match="STRATEGIC_REVIEW_FACT"):
        parse_advice(json.dumps(value), position(), facts=facts())


def test_review_preserves_named_evidence_without_certifying_interpretation():
    value = assessment()
    value["review"]["materialAssumptions"] = [
        {
            "assessment": "Greece's population may make a race for land worthwhile.",
            "status": "inferred",
            "facts": [{"id": "rival/Greece/totalPopulation", "value": 3}],
        }
    ]
    parsed = parse_advice(json.dumps(value), position(), facts=facts())
    assumption = parsed["review"]["materialAssumptions"][0]
    assert assumption["status"] == "inferred"
    assert assumption["facts"][0]["metric"] == "totalPopulation"
    assert assumption["facts"][0]["observedTurn"] == 20
    assumption["source"] = "review:20:1"
    pinned = RecordStore.pin({"records": {}, "strategicAdvice": {"asOfTurn": 20, **parsed}}, ["review:20:1"])
    assert pinned[0]["assessment"]["facts"][0]["metric"] == "totalPopulation"
    assert pinned[0]["assessment"]["status"] == "inferred"


def test_unsupported_assumptions_remain_unknown():
    with pytest.raises(ValueError):
        MaterialAssumption(assessment="A rival owns three cities", status="inferred", facts=[])
    assert MaterialAssumption(assessment="A rival's total city count is unknown", status="unknown", facts=[])


def test_fact_references_are_unique():
    value = assessment()
    reference = {"id": "rival/Greece/totalPopulation", "value": 3}
    value["review"]["materialAssumptions"] = [
        {"assessment": "Population matters", "status": "inferred", "facts": [reference, reference]}
    ]
    with pytest.raises(ValueError, match="STRATEGIC_REVIEW_FACT"):
        parse_advice(json.dumps(value), position(), facts=facts())


def test_runtime_facts_use_permitted_raw_snapshot_before_overview_compaction():
    raw = position()
    # Review startup's overview removes map tiles and flattens city development.
    compact = overview(raw)
    result = review_named_facts(
        {
            "civilization": "France",
            "observation": raw,
            "reads": {
                "player_turn": compact,
                "victory_progress": {
                    "metrics": [{"name": "Population"}],
                    "rankings": {"rows": [{"civilization": "Greece", "values": [3]}]},
                },
            },
        }
    )
    values = {fact["id"]: fact["value"] for fact in result}
    assert values["rival/Greece/visibleCityCountLowerBound"] == 1
    assert values["rival/Greece/observedCityLocationsCount"] == 2
    assert values["city/ours/population"] == 3


def test_adopting_review_preserves_dated_sources_after_later_reviews():
    value = assessment()
    value["review"]["materialAssumptions"] = [
        {
            "assessment": "Greece's population may make expansion worthwhile.",
            "status": "inferred",
            "facts": [{"id": "rival/Greece/totalPopulation", "value": 3}],
        }
    ]
    advice = parse_advice(json.dumps(value), position(), facts=facts())
    advice["review"]["materialAssumptions"][0]["source"] = "review:20:1"
    store = RecordStore(None)
    content = {
        "type": "strategy",
        "assessment": "Expand",
        "assumptions": [{"text": "Our interpretation of Greece's growth", "source": "review:20:1"}],
    }
    adopted = store.content(content, {"records": {}, "strategicAdvice": {"asOfTurn": 20, **advice}})
    replaced = store.content(content, {"records": {}, "strategicAdvice": None}, adopted)
    assert not replaced["unverifiedSources"]
    evidence = replaced["_assumptions"][0]
    assert evidence["reviewTurn"] == 20
    assert evidence["assessment"]["facts"][0]["observedTurn"] == 20
    assert evidence["assessment"]["status"] == "inferred"
    visible = public_record("mem1", {"category": "strategy", "body": replaced})["reviewEvidence"][0]
    assert visible["facts"][0]["metric"] == "totalPopulation"
    assert visible["source"] == "review:20:1" and visible["reviewTurn"] == 20
