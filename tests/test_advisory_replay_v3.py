import copy
from pathlib import Path

import pytest

from civ_agent.advisory_replay import calibrate, load_cases, report
from civ_agent.decisions_policy import DecisionsSettings
from civ_agent.decisions_tasks import parse_answers, request_body, request_metadata
from tests.test_advisory_replay import captured

FIXTURE = Path(__file__).parent / "fixtures/decisions-advisory-replay.json"


def cases():
    return load_cases(FIXTURE)


def test_production_shaped_labels_cover_all_tasks_and_observed_information_limits():
    fixture = cases()
    assert {
        "retrieval_include",
        "retrieval_priority",
        "preservation",
        "beliefs",
        "early_review",
        "city_focus",
        "city_construction",
    } == {name.split(":")[0] for case in fixture for name in case["labels"]}
    assert {"calibration", "holdout"} == {case["split"] for case in fixture}
    assert all(
        case["evidence"]["player"] == "Rome" and case["provenance"]["observedGameplay"] is False for case in fixture
    )
    source = next(case for case in fixture if case["id"] == "preserve-unresolved")["evidence"]["preservation"][0]
    assert source["record"]["body"]["body"]["text"] == source["source"]["text"]
    assert source["source"]["public"] is False
    memory = next(case for case in fixture if case["id"] == "retrieve-relevant-note")["evidence"]["retrieval"][0]
    assert memory["record"]["state"] == "active" and memory["record"]["text"]
    positive = next(case for case in fixture if case["id"] == "belief-visible-counterevidence")
    assert (
        positive["evidence"]["newEvidence"][0]["record"]["body"]["current"]["hostiles"][0]["civilization"] == "Greece"
    )
    review = next(case for case in fixture if case["id"] == "review-recorded-city-loss")["evidence"]["earlyReview"]
    assert review["baseline"]["available"] and review["deltas"]["comparisonComplete"]
    assert review["current"]["source"] == {"key": "observation:overview", "version": 2}
    assert review["baseline"]["reviewTurn"] == review["baseline"]["sourceTurn"] == 10
    assert review["sourceTurn"] <= review["completedTurn"] < review["boundaryTurn"] < review["nextReviewTurn"]


def test_mixed_good_weak_refused_malformed_and_missing_answers_keep_true_statuses_and_delivery_separate():
    case = next(case for case in cases() if case["id"] == "mixed-independent-tasks")
    body = request_body(case["evidence"], DecisionsSettings(decisions_enabled=True))
    provider = [
        {"name": name, "type": "choice", "choice": values[0], "confidence": 0.99}
        for name, values in case["labels"].items()
        if name != "early_review"
    ]
    for answer in provider:
        if answer["name"] == "retrieval_include:0":
            answer["confidence"] = 0.2
        elif answer["name"] == "retrieval_priority:0":
            answer.clear()
            answer.update(name="retrieval_priority:0", type="refusal")
        elif answer["name"] == "preservation:0":
            answer["choice"] = "invented-value"
    response = {**request_metadata(body, case["evidence"]), **parse_answers(body, {"answers": provider}, 0.90)}
    result = report([case], {case["id"]: response})
    assert result["questions"]["statuses"] == {
        "accepted": 3,
        "low_confidence": 1,
        "refusal": 1,
        "malformed": 1,
        "missing": 1,
    }
    assert result["labelMatches"] == 3 and result["rawLabelMatches"] == 4
    assert result["byTask"]["retrieval"]["fixtureQuestions"] == 2
    assert result["byQuestionType"]["retrieval_include"]["confidence"]["median"] == 0.2
    assert result["delivery"]["deliveredQuestions"] == 0
    delivered = report(
        [case],
        {case["id"]: response},
        deliveries={case["id"]: {"deliveredNames": ["city_focus:0"], "staleNames": ["beliefs:0"]}},
    )
    assert delivered["delivery"] == {"casesWithObservedEvidence": 1, "deliveredQuestions": 1, "staleQuestions": 1}
    invalid = report(
        [case], {case["id"]: response}, deliveries={case["id"]: {"deliveredNames": ["retrieval_include:0"]}}
    )
    assert invalid["cases"][0]["delivery"]["status"] == "inconsistent_evidence"
    assert invalid["delivery"]["deliveredQuestions"] == 0


@pytest.mark.parametrize("field", ["requestId", "model", "policyVersion", "inputHash", "promptHash", "questionNames"])
def test_replay_refuses_unmatched_request_identity(field):
    case = cases()[0]
    response = captured(case)
    response[field] = [] if field == "questionNames" else "different"
    result = report([case], {case["id"]: response})
    assert result["identityMismatches"] == 1 and result["identityVerifiedCases"] == 0
    assert result["labelMatches"] == result["rawLabelMatches"] == 0


def test_evidence_change_rejects_previously_matching_response_and_diagnostics_tampering_cannot_accept():
    case = cases()[0]
    response = captured(case)
    modified = copy.deepcopy(case)
    modified["evidence"]["player"] = "France"
    assert report([modified], {case["id"]: response})["identityMismatches"] == 1
    response["rawAnswers"][0]["confidence"] = 0.1
    result = report([case], {case["id"]: response})
    assert result["labelMatches"] == 0
    assert result["cases"][0]["fallback"] == "malformed_broker_output"


def test_calibration_only_rethresholds_validated_original_answers_without_inventing_refusals():
    calibration = next(case for case in cases() if case["id"] == "preserve-unresolved")
    holdout = next(case for case in cases() if case["id"] == "preserve-acknowledgement")
    body = request_body(calibration["evidence"], DecisionsSettings(decisions_enabled=True))
    response = {
        **request_metadata(body, calibration["evidence"]),
        **parse_answers(
            body,
            {"answers": [{"name": "preservation:0", "type": "choice", "choice": "consider", "confidence": 0.75}]},
            0.90,
        ),
    }
    result = calibrate(
        [calibration, holdout], {calibration["id"]: response, holdout["id"]: captured(holdout)}, thresholds=(0.7, 0.9)
    )
    assert result["selectedThreshold"] is None and result["holdoutSelectionUsed"] is False
    assert result["candidates"][0]["questions"]["acceptedQuestions"] == 1
    assert result["candidates"][1]["questions"]["statuses"] == {"low_confidence": 1}
    assert all(row["questions"]["fixtureQuestions"] == 1 for row in result["candidates"])


def test_fixture_truncation_and_city_alternatives_match_actual_builder_limits():
    fixture = {case["id"]: case for case in cases()}
    review = fixture["review-truncated-city-list"]["evidence"]["earlyReview"]
    for projection in (review["baseline"], review["current"]):
        assert projection["coverage"]["listLimit"] == 64
        assert len(projection["facts"]["cities"]) + projection["coverage"]["omitted"]["cities"] == 65
    assert not review["deltas"]["comparisonComplete"]
    truncated = fixture["belief-truncated-absence"]["evidence"]["newEvidence"][0]
    assert len(truncated["record"]["body"]["current"]["recordedUnitFacts"]) == 16
    assert truncated["coverage"]["omitted"]["recordedUnitFacts"] == 3
    assert "hostiles" in truncated["coverage"]["unknownFields"]
    for case in fixture.values():
        for city in case["evidence"].get("cities", []):
            assert city["focuses"]
            context = next(c for c in case["evidence"]["cityContext"]["cities"] if c["handle"] == city["city"])
            assert city["facts"]["queue"] == context["constructionQueue"]
    assert fixture["mixed-independent-tasks"]["provenance"]["kind"] == "synthetic_schema_stress"
