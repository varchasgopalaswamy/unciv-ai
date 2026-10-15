import json

import pytest
from sqlalchemy import select

from civ_agent.diplomacy_decisions import TWO_QUESTION_POLICY_VERSION, parse_answer
from civ_agent.experiment_status import classifier_status, routing_classifier_summary
from civ_agent.protocol import JsonObject
from civ_agent.telemetry import JobAccounting, classifier_calls
from tests.test_routing_two_questions import binary_answer


def answer(direction, choice, confidence) -> JsonObject:
    return parse_answer(
        direction,
        {"answers": [{"name": "delivery", "type": "choice", "choice": choice, "confidence": confidence}]},
    )


def test_routing_evidence_survives_database_reopen_without_provider_or_player_text(tmp_path):
    probabilities = [
        {"value": "respond_now", "probability": 0.1, "text": "PRIVATE TEXT"},
        {"value": "batch", "probability": 0.8},
        {"value": "no_separate_response", "probability": 0.1},
    ]
    result = answer("incoming", "batch", 0.72)
    result.update(
        model="gpt-6-luna",
        policyVersion="diplomacy-routing-v2",
        probabilities=probabilities,
        thresholds={"batch": 0.9, "no_separate_response": 0.98, "PRIVATE TEXT": 0.1},
        inputHash="a" * 64,
        promptHash="b" * 64,
        contextSchemaVersion=2,
        requestId="c" * 64,
        input={"pending": ["PRIVATE TEXT"]},
        response={"text": "PRIVATE TEXT"},
        providerError={"kind": "authentication", "httpStatus": 401, "body": "PRIVATE TEXT"},
        usage={
            "input_tokens": 100,
            "total_tokens": 100,
            "text": "PRIVATE TEXT",
            "input_tokens_details": {"cached_tokens": 5, "text": "PRIVATE TEXT"},
        },
        accounting={
            "authority": "d" * 64,
            "rateVersion": "decisions-standard-2026-10-14-v1",
            "limitKind": "local_estimated_spending_ceiling",
            "spentNanodollars": 10000,
            "requests": 1,
            "input": "PRIVATE TEXT",
        },
    )
    result["rawAnswer"].update(text="PRIVATE TEXT", probabilities=probabilities, evidence={"text": "PRIVATE TEXT"})
    accounting = JobAccounting(tmp_path)
    accounting.classifier("incoming", result)
    accounting.engine.dispose()
    reopened = JobAccounting(tmp_path)
    try:
        with reopened.connect() as db:
            payload = db.scalar(select(classifier_calls.c.payload))
        assert "PRIVATE TEXT" not in payload
        report = reopened.classifier_reports()[0]
        assert report["choice"] == "respond_now" and report["rawChoice"] == "batch"
        assert report["confidence"] == 0.72 and report["appliedThreshold"] == 0.9
        assert report["overridden"] is True and report["lowConfidence"] is True
        assert report["thresholds"] == {"batch": 0.9, "no_separate_response": 0.98}
        assert report["inputHash"] == "a" * 64 and report["promptHash"] == "b" * 64
        assert report["contextSchemaVersion"] == 2
        assert report["rawAnswer"] == {
            "name": "delivery",
            "type": "choice",
            "choice": "batch",
            "confidence": 0.72,
            "probabilities": [{"value": item["value"], "probability": item["probability"]} for item in probabilities],
        }
        assert report["probabilities"] == report["rawAnswer"]["probabilities"]
        assert report["usage"] == {
            "input_tokens": 100,
            "total_tokens": 100,
            "input_tokens_details": {"cached_tokens": 5},
        }
        assert report["providerError"] == {"kind": "authentication", "httpStatus": 401}
        assert report["accounting"]["requests"] == 1
        assert "PRIVATE TEXT" not in json.dumps(classifier_status(reopened.path))
    finally:
        reopened.engine.dispose()


def test_routing_status_separates_raw_choices_overrides_confidence_and_legacy_unknowns(tmp_path):
    accounting = JobAccounting(tmp_path)
    try:
        for direction, result in (
            ("incoming", answer("incoming", "respond_now", 0.2)),
            ("incoming", answer("incoming", "batch", 0.72)),
            ("incoming", answer("incoming", "batch", 0.91)),
            ("outgoing", answer("outgoing", "omit", 0.95)),
            ("incoming", {"choice": "respond_now", "confidence": None, "fallback": "timeout"}),
            ("advisory_batch", {"confidence": 0.99, "fallback": None, "usage": {"input_tokens": 55}}),
            ("incoming", {"choice": "respond_now", "fallback": "protected", "confidence": 1}),
        ):
            accounting.classifier(direction, result)
        # These exact old rows predate raw choices and confidence accounting.
        with accounting.connect() as db:
            for direction, choice, fallback in (
                ("incoming", "respond_now", "low_confidence"),
                ("outgoing", "send_now", None),
            ):
                db.execute(
                    classifier_calls.insert().values(
                        id="legacy-" + direction,
                        payload=json.dumps({"atMs": 1, "direction": direction, "choice": choice, "fallback": fallback}),
                    )
                )
        status = classifier_status(accounting.path)
        routing = status["routing"]
        assert status["calls"] == 9 and status["usage"] == {"input_tokens": 55}
        assert routing["calls"] == 7
        assert routing["excluded"] == {"advisoryBatch": 1, "protectedOrDeadlineBypass": 1, "otherDirections": 0}
        assert routing["rawDispositions"] == {"respond_now": 1, "batch": 2, "omit": 1}
        assert routing["effectiveDispositions"] == {"respond_now": 4, "batch": 1, "omit": 1, "send_now": 1}
        assert routing["rawDispositionUnknown"] == 3 and routing["effectiveDispositionUnknown"] == 0
        assert routing["genuineOverrides"] == 1 and routing["overrideUnknown"] == 3
        assert routing["lowConfidenceImmediate"] == 1
        assert routing["lowConfidence"] == 2 and routing["lowConfidenceUnknown"] == 3
        assert routing["confidence"] == {
            "known": 4,
            "unknown": 3,
            "mean": pytest.approx(0.695),
            "median": pytest.approx(0.815),
            "min": 0.2,
            "max": 0.95,
            "bins": {"<0.50": 1, "0.50-<0.70": 0, "0.70-<0.80": 1, "0.80-<0.90": 0, ">=0.90": 2},
        }
        incoming, outgoing = routing["byDirection"]["incoming"], routing["byDirection"]["outgoing"]
        assert incoming["calls"] == 5 and outgoing["calls"] == 2
        assert incoming["confidence"]["known"] == 3 and incoming["confidence"]["unknown"] == 2
        assert outgoing["confidence"]["known"] == 1 and outgoing["confidence"]["unknown"] == 1
        assert outgoing["genuineOverrides"] == 0 and outgoing["overrideUnknown"] == 1
    finally:
        accounting.engine.dispose()


def test_malformed_routing_metadata_never_copies_arbitrary_nested_text(tmp_path):
    accounting = JobAccounting(tmp_path)
    try:
        accounting.classifier(
            "outgoing",
            {
                "choice": "send_now",
                "confidence": float("nan"),
                "appliedThreshold": True,
                "overridden": "PRIVATE TEXT",
                "lowConfidence": {"text": "PRIVATE TEXT"},
                "rawChoice": ["PRIVATE TEXT"],
                "rawAnswer": {"name": "delivery", "type": "choice", "choice": ["PRIVATE TEXT"], "confidence": 1},
                "probabilities": [{"value": "PRIVATE TEXT", "probability": 1}],
                "thresholds": {"batch": False, "omit": float("inf"), "extra": "PRIVATE TEXT"},
                "inputHash": "PRIVATE TEXT",
                "promptHash": {"input": "PRIVATE TEXT"},
                "contextSchemaVersion": "PRIVATE TEXT",
                "providerError": {"kind": {"input": "PRIVATE TEXT"}, "httpStatus": "PRIVATE TEXT"},
                "usage": {"input_tokens": "PRIVATE TEXT", "output_tokens": False},
            },
        )
        report = accounting.classifier_reports()[0]
        assert "PRIVATE TEXT" not in json.dumps(report)
        assert not {"confidence", "rawChoice", "rawAnswer", "probabilities", "overridden", "inputHash"} & report.keys()
        routing = classifier_status(accounting.path)["routing"]
        assert routing["confidence"]["unknown"] == 1 and routing["overrideUnknown"] == 1
    finally:
        accounting.engine.dispose()


def test_confidence_bins_use_explicit_boundaries_and_empty_samples_stay_unknown():
    reports = [
        {"direction": "outgoing", **answer("outgoing", "send_now", value)} for value in (0.0, 0.5, 0.7, 0.8, 0.9, 1.0)
    ]
    summary = routing_classifier_summary(reports)
    assert summary["confidence"]["bins"] == {
        "<0.50": 1,
        "0.50-<0.70": 1,
        "0.70-<0.80": 1,
        "0.80-<0.90": 1,
        ">=0.90": 2,
    }
    assert summary["byDirection"]["incoming"]["confidence"] == {
        "known": 0,
        "unknown": 0,
        "mean": None,
        "median": None,
        "min": None,
        "max": None,
        "bins": {"<0.50": 0, "0.50-<0.70": 0, "0.70-<0.80": 0, "0.80-<0.90": 0, ">=0.90": 0},
    }


def test_two_question_accounting_keeps_intent_and_action_confidence_separate_after_reopen(tmp_path):
    raw = [binary_answer("response_needed", "not_needed", 0.4), binary_answer("response_timing", "can_wait", 0.95)]
    result = parse_answer("incoming", {"answers": raw}, policy_version=TWO_QUESTION_POLICY_VERSION)
    result["policyVersion"] = TWO_QUESTION_POLICY_VERSION
    for question in result["rawAnswers"]:
        question["providerText"] = "PRIVATE TEXT"
        question["probabilities"][0]["providerText"] = "PRIVATE TEXT"
    result["rawAnswer"] = {"name": "delivery", "type": "choice", "choice": "batch", "confidence": 1}
    result["probabilities"] = [
        {"value": "respond_now", "probability": 0.01},
        {"value": "batch", "probability": 0.98},
        {"value": "no_separate_response", "probability": 0.01},
    ]
    accounting = JobAccounting(tmp_path)
    accounting.classifier("incoming", result)
    accounting.classifier("incoming", {**answer("incoming", "batch", 0.99), "policyVersion": "diplomacy-routing-v2"})
    accounting.engine.dispose()
    reopened = JobAccounting(tmp_path)
    try:
        report = reopened.classifier_reports()[0]
        assert report["rawChoice"] == "no_separate_response" and report["rawChoiceConfidence"] == 0.4
        assert report["choice"] == "batch" and report["confidence"] == 0.95
        assert report["questionBasis"] == "response_timing"
        assert report["probabilities"] is None and "rawAnswer" not in report
        assert report["rawAnswers"] == raw
        assert "PRIVATE TEXT" not in json.dumps(reopened.classifier_reports())
        routing = classifier_status(reopened.path)["routing"]
        assert routing["questionBasis"] == {"response_timing": 1}
        assert routing["confidenceByPolicy"][TWO_QUESTION_POLICY_VERSION]["mean"] == 0.95
        assert routing["confidenceByPolicy"]["diplomacy-routing-v2"]["mean"] == 0.99
        assert routing["perQuestion"]["response_needed"]["choices"] == {"not_needed": 1}
        assert routing["perQuestion"]["response_needed"]["confidence"]["mean"] == 0.4
        assert routing["perQuestion"]["response_timing"]["choices"] == {"can_wait": 1}
        assert routing["perQuestion"]["response_timing"]["confidence"]["mean"] == 0.95
        assert routing["rawChoiceConfidence"]["known"] == 1 and routing["rawChoiceConfidence"]["unknown"] == 1
        assert routing["genuineOverrides"] == 1
    finally:
        reopened.engine.dispose()


@pytest.mark.parametrize("invalid", ["duplicate", "unknown_question", "invalid_choice"])
def test_two_question_accounting_rejects_malformed_or_unknown_answer_shapes(tmp_path, invalid):
    raw = [binary_answer("response_needed", "not_needed", 0.4), binary_answer("response_timing", "can_wait", 0.95)]
    if invalid == "duplicate":
        raw[1] = raw[0]
    elif invalid == "unknown_question":
        raw[1]["name"] = "PRIVATE TEXT"
    else:
        raw[1]["choice"] = "PRIVATE TEXT"
    accounting = JobAccounting(tmp_path)
    try:
        accounting.classifier(
            "incoming",
            {
                "policyVersion": TWO_QUESTION_POLICY_VERSION,
                "rawAnswers": raw,
                "questionBasis": "PRIVATE TEXT",
                "rawChoiceConfidence": "PRIVATE TEXT",
                "input": "PRIVATE TEXT",
            },
        )
        report = accounting.classifier_reports()[0]
        assert "rawAnswers" not in report and "questionBasis" not in report
        assert report["probabilities"] is None and "PRIVATE TEXT" not in json.dumps(report)
    finally:
        accounting.engine.dispose()


def test_two_question_refusal_accounting_never_retains_provider_reason(tmp_path):
    accounting = JobAccounting(tmp_path)
    try:
        accounting.classifier(
            "incoming",
            {
                "policyVersion": TWO_QUESTION_POLICY_VERSION,
                "fallback": "refusal",
                "rawAnswers": [
                    {"name": "response_needed", "type": "refusal", "reason": "PRIVATE TEXT"},
                    binary_answer("response_timing", "can_wait", 0.99),
                ],
            },
        )
        report = accounting.classifier_reports()[0]
        assert report["rawAnswers"][0] == {"name": "response_needed", "type": "refusal"}
        assert "PRIVATE TEXT" not in json.dumps(report)
        status = classifier_status(accounting.path)["routing"]["perQuestion"]
        assert status["response_needed"]["refusals"] == 1
        assert status["response_needed"]["confidence"]["unknown"] == 1
        assert status["response_timing"]["confidence"]["known"] == 1
    finally:
        accounting.engine.dispose()
