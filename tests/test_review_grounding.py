"""Reviews cite the permitted observations they actually received."""

import json

import pytest

from civ_agent.memory_records import public_record
from civ_agent.memory_store import RecordStore
from civ_agent.review_grounding import MaterialAssumption, ReviewEvidence, named_facts
from civ_agent.strategic_review import parse_advice
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


def assumption_report(source, *, status="inferred"):
    value = assessment()
    value["review"]["materialAssumptions"] = [
        {"assessment": "Founding here avoids travel and offers useful luxuries.", "status": status, "sources": source}
    ]
    return value


def test_population_never_establishes_foreign_city_count_or_current_stale_sightings():
    values = {fact["id"]: fact for fact in facts()}
    assert values["rival/Greece/totalPopulation"]["value"] == 3
    assert values["rival/Greece/empireCityCount"]["value"] is None
    assert values["rival/Greece/visibleCityCountLowerBound"]["value"] == 1
    assert values["rival/Greece/observedCityLocationsCount"]["value"] == 2
    assert values["our/empireCityCount"]["value"] == 1
    assert not any(fact["subject"] == "Sparta" for fact in facts())


def test_opening_settlement_report_can_cite_rich_read_without_inventing_fact_paths():
    evidence = ReviewEvidence({"observation": {"turn": 0}})
    settlement = {
        "settlement": {
            "candidates": [
                {
                    "siteStatus": "ELIGIBLE",
                    "route": {"estimatedTurns": 0},
                    "score": 165,
                    "unclaimedLuxuryHappinessIfConnected": 8,
                }
            ]
        }
    }
    delivered = evidence.deliver("unit_details", settlement, {"unit": "unit2", "sections": ["settlement"]})
    report = parse_advice(json.dumps(assumption_report([delivered["source"]])), {"turn": 0}, evidence=evidence)
    source = report["review"]["materialAssumptions"][0]["sources"][0]
    assert source["name"] == "unit_details" and source["observedTurn"] == 0
    assert source["arguments"]["unit"] == "unit2"
    assert "data" not in source
    assert report["_evidence"][source["id"]]["data"] == settlement
    assert delivered["data"] == settlement


@pytest.mark.parametrize("kind", ["unknown", "undelivered", "other_job", "duplicate", "memory_evidence"])
def test_report_rejects_sources_not_delivered_to_this_review(kind):
    evidence = ReviewEvidence({"observation": position()})
    observation_id = "observation:detail/bf945872283986ad"
    ours = evidence.deliver("player_turn", {"cities": [], "evidence": observation_id})["source"]
    foreign = ReviewEvidence({"observation": position()}).deliver("player_turn", {"cities": []})["source"]
    refs = {
        "unknown": ["invented"],
        "undelivered": ["settlement/candidates/0/score"],
        "other_job": [foreign],
        "duplicate": [ours, ours],
        "memory_evidence": [observation_id],
    }[kind]
    with pytest.raises(ValueError, match="STRATEGIC_REVIEW_SOURCE"):
        parse_advice(json.dumps(assumption_report(refs)), position(), evidence=evidence)


def test_sources_are_immutable_deduplicated_and_only_retained_when_cited():
    evidence = ReviewEvidence({"observation": position()})
    data = {"settlement": {"score": 165}}
    first = evidence.deliver("unit_details", data, {"unit": "unit2"})
    again = evidence.deliver("unit_details", data, {"unit": "unit2"})
    unused = evidence.deliver("unit_details", {"hiddenToModel": "not cited"}, {"unit": "unit1"})
    assert first == again
    data["settlement"]["score"] = -1
    first["data"]["settlement"]["score"] = -2
    value = assumption_report([first["source"]])
    value["review"]["materialAssumptions"] *= 2
    report = parse_advice(json.dumps(value), position(), evidence=evidence)
    assert set(report["_evidence"]) == {first["source"]}
    assert unused["source"] not in report["_evidence"]
    assert report["_evidence"][first["source"]]["data"]["settlement"]["score"] == 165


def test_unsupported_assumptions_remain_unknown():
    with pytest.raises(ValueError):
        MaterialAssumption(assessment="A rival owns three cities", status="inferred", sources=[])
    assert MaterialAssumption(assessment="A rival's total city count is unknown", status="unknown", sources=[])


def test_adopting_review_retains_compact_dated_sources_after_later_reviews():
    evidence = ReviewEvidence({"observation": position()})
    delivered = evidence.deliver("victory_progress", {"Population": 3})
    advice = parse_advice(json.dumps(assumption_report([delivered["source"]])), position(), evidence=evidence)
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
    visible = public_record("mem1", {"category": "strategy", "body": replaced})["reviewEvidence"][0]
    assert visible["source"] == "review:20:1" and visible["reviewTurn"] == 20
    source = visible["sources"][0]
    assert source["observedTurn"] == 20 and source["sha256"]
    assert "data" not in source and "_evidence" not in visible


def test_exact_source_archive_and_compact_adopted_evidence_survive_checkpoint(tmp_path):
    from sqlalchemy import select

    from civ_agent import memory_schema as tables
    from civ_agent.review_store import ReviewStore
    from tests.domain_fixtures import setup

    memory = setup(tmp_path)
    store = ReviewStore(memory)
    evidence = ReviewEvidence({"observation": position()})
    original = {"settlement": {"score": 165, "siteStatus": "ELIGIBLE"}}
    delivered = evidence.deliver("unit_details", original, {"unit": "unit2"})
    report = parse_advice(json.dumps(assumption_report([delivered["source"]])), position(), evidence=evidence)
    store.prepare("review20", {"turn": 20, "observation": position()}, {}, 60)
    store.complete("review20", report)
    snapshot = memory.snapshot("gameplay", 21)
    assert "_evidence" not in snapshot["strategicAdvice"]
    assert "siteStatus" not in json.dumps(snapshot["strategicAdvice"])
    memory.finish_job("game", True)
    memory.start_job("adopt", "gameplay", {}, game_turn=21)
    read = memory.history(20, job="adopt", include_review=True)
    assert read["reviewSources"] == ["review:20:1"]
    result = memory.patch(
        "adopt",
        [
            {
                "op": "create",
                "content": {
                    "type": "strategy",
                    "assessment": "Settle here",
                    "assumptions": [{"text": "The current site is promising", "source": "review:20:1"}],
                },
            }
        ],
    )
    assert not result.get("error")
    checkpoint = tmp_path / "review-sources.json"
    digest = memory.backup(checkpoint)
    store.prepare("review25", {"turn": 25, "observation": {"turn": 25}}, {}, 60)
    store.complete("review25", assessment())
    memory.restore(checkpoint, digest, "restored")
    with memory.transaction() as db:
        archived = json.loads(
            db.execute(select(tables.reviews.c.advice).where(tables.reviews.c.job == "review20")).scalar_one()
        )
        assert archived["_evidence"][delivered["source"]]["data"] == original
        records = RecordStore(db).all(categories={"strategy"}, project=False)
        adopted = next(iter(records.values()))
        visible = public_record("mem1", adopted)["reviewEvidence"][0]
        assert visible["sources"][0]["id"] == delivered["source"]
        assert "data" not in visible["sources"][0]
        assert visible["source"] == "review:20:1"
