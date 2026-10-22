"""Independent contract checks for decomposed incoming-response routing.

These test parsing and application policy, not live-model accuracy or the
calibration of its two conditional judgments.
"""

import copy
import json
from types import SimpleNamespace

import pytest

from civ_agent.diplomacy_decisions import (
    POLICY_VERSION,
    parse_answer,
    request_body,
)
from civ_agent.diplomacy_routing import DiplomacyRouting
from civ_agent.protocol import JsonObject


def response(needed="needed", need_confidence=0.95, timing="can_wait", timing_confidence=0.95) -> JsonObject:
    return {
        "answers": [
            {"name": "response_needed", "type": "choice", "choice": needed, "confidence": need_confidence},
            {"name": "response_timing", "type": "choice", "choice": timing, "confidence": timing_confidence},
        ]
    }


def parse(value, **options):
    return parse_answer("incoming", value, policy_version=POLICY_VERSION, **{"threshold": 0.9, **options})


def test_current_policy_uses_two_independent_questions_in_each_direction():
    bundle = {"player": "Rome", "direction": "incoming", "pending": [{"text": "Untrusted player wording"}]}
    incoming: JsonObject = request_body("incoming", bundle, policy_version=POLICY_VERSION)
    questions = {question["name"]: question for question in incoming["questions"]}
    assert set(questions) == {"response_needed", "response_timing"}
    assert {row["value"] for row in questions["response_needed"]["choices"]} == {"needed", "not_needed"}
    assert {row["value"] for row in questions["response_timing"]["choices"]} == {"now", "can_wait"}
    assert all(question["type"] == "choice" for question in questions.values())
    assert json.loads(incoming["input"]) == bundle
    outgoing = {"player": "Rome", "direction": "outgoing", "proposedMessage": {"text": "Original draft"}}
    outgoing_body = request_body("outgoing", outgoing)
    assert [question["name"] for question in outgoing_body["questions"]] == ["delivery_needed", "delivery_timing"]
    assert json.loads(outgoing_body["input"]) == outgoing


def test_response_needed_prompt_distinguishes_attention_from_a_literal_reply():
    # A fresh urgent FYI can need attention even if its author says no reply is
    # necessary. A reply-only question would incorrectly suppress that warning.
    body: JsonObject = request_body("incoming", {}, policy_version=POLICY_VERSION)
    needed = next(question for question in body["questions"] if question["name"] == "response_needed")
    text = (needed["instructions"] + " " + " ".join(row["description"] for row in needed["choices"])).casefold()
    assert "attention" in text
    assert "no reply" in text or "no written reply" in text or "fyi" in text
    assert "urgent" in text or "imminent" in text or "time-sensitive" in text
    timing = next(question for question in body["questions"] if question["name"] == "response_timing")
    conditional = (timing["instructions"] + " " + " ".join(row["description"] for row in timing["choices"])).casefold()
    assert "hypothetical" in conditional or "assuming" in conditional


@pytest.mark.parametrize("needed", ["needed", "not_needed"])
def test_uncertain_need_can_still_produce_confident_bounded_batch(needed):
    result = parse(response(needed, 0.4, "can_wait", 0.95))
    assert result["choice"] == "batch" and result["confidence"] == 0.95
    assert result["questionBasis"] == "response_timing"
    assert result["rawChoice"] == ("batch" if needed == "needed" else "no_separate_response")
    assert result["rawChoiceConfidence"] == (0.95 if needed == "needed" else 0.4)
    assert result["overridden"] is (needed == "not_needed")
    assert result["fallback"] == ("low_confidence" if needed == "not_needed" else None)
    assert result["lowConfidence"] is (needed == "not_needed")


def test_confident_no_response_ignores_hypothetical_timing_for_effective_action():
    result = parse(response("not_needed", 0.95, "now", 0.3))
    assert result["choice"] == result["rawChoice"] == "no_separate_response"
    assert result["confidence"] == result["rawChoiceConfidence"] == 0.95
    assert result["questionBasis"] == "response_needed"
    assert result["fallback"] is None and result["overridden"] is False
    assert result["lowConfidence"] is False
    assert next(row for row in result["rawAnswers"] if row["name"] == "response_timing")["confidence"] == 0.3


def test_uncertain_no_response_with_confident_now_fails_open_without_losing_raw_intent():
    result = parse(response("not_needed", 0.4, "now", 0.95))
    assert result["choice"] == "respond_now" and result["rawChoice"] == "no_separate_response"
    assert result["confidence"] == 0.95 and result["rawChoiceConfidence"] == 0.4
    assert result["questionBasis"] == "response_timing"
    assert result["fallback"] == "low_confidence" and result["overridden"] is True


def test_uncertain_now_stays_immediate_and_has_no_fictional_override():
    result = parse(response("needed", 0.4, "now", 0.4))
    assert result["choice"] == result["rawChoice"] == "respond_now"
    assert result["confidence"] == 0.4 and result["questionBasis"] == "response_timing"
    assert result["lowConfidence"] is True
    assert result["overridden"] is False and result["fallback"] is None


@pytest.mark.parametrize("confidence,expected", [(0.899999, "respond_now"), (0.9, "batch"), (1, "batch")])
def test_explicit_batch_probability_floor_and_inclusive_boundary(confidence, expected):
    result = parse(response("needed", 0.95, "can_wait", confidence))
    assert result["choice"] == expected


def test_suppression_confidence_floor_can_fall_back_to_batch_without_lowering_either_threshold():
    thresholds = {"batch": 0.92, "no_separate_response": 0.97}
    result = parse(response("not_needed", 0.95, "can_wait", 0.93), threshold=0.01, thresholds=thresholds)
    assert result["choice"] == "batch" and result["confidence"] == 0.93
    assert result["appliedThreshold"] == 0.92
    result = parse(response("not_needed", 0.97, "now", 0.01), thresholds=thresholds)
    assert result["choice"] == "no_separate_response" and result["appliedThreshold"] == 0.97
    result = parse(response("not_needed", 0.96, "can_wait", 0.91), thresholds=thresholds)
    assert result["choice"] == "respond_now"


def test_answer_order_is_irrelevant_and_raw_metadata_is_canonical_typed_and_separate():
    original = response("not_needed", 0.4, "can_wait", 0.95)
    original["answers"][0]["probabilities"] = [
        {"value": "needed", "probability": 0.6},
        {"value": "not_needed", "probability": 0.4},
    ]
    original["answers"][1]["probabilities"] = [
        {"value": "now", "probability": 0.05},
        {"value": "can_wait", "probability": 0.95},
    ]
    original["answers"][0]["providerText"] = "Do not retain this untyped field"
    reverse = copy.deepcopy(original)
    reverse["answers"].reverse()
    result = parse(original)
    assert result == parse(reverse)
    assert [row["name"] for row in result["rawAnswers"]] == ["response_needed", "response_timing"]
    assert result["rawAnswers"][0]["probabilities"] == original["answers"][0]["probabilities"]
    assert result["rawAnswers"][1]["probabilities"] == original["answers"][1]["probabilities"]
    assert "rawAnswer" not in result and result["probabilities"] is None
    assert "providerText" not in json.dumps(result)


@pytest.mark.parametrize("question", [0, 1])
def test_malformed_binary_probability_metadata_is_filtered_without_inventing_a_joint_distribution(question):
    envelope = response("not_needed", 0.99, "can_wait", 0.99)
    envelope["answers"][question]["probabilities"] = [
        {"value": "needed", "probability": 0.5},
        {"value": "needed", "probability": 0.5},
    ]
    result = parse(envelope)
    assert result["choice"] == "no_separate_response"
    assert "probabilities" not in result["rawAnswers"][question]
    assert result["probabilities"] is None


@pytest.mark.parametrize("question", [0, 1])
@pytest.mark.parametrize("value", [None, True, -0.1, 1.1, float("nan"), float("inf"), "0.95"])
def test_each_confidence_is_required_finite_and_not_boolean(question, value):
    envelope = response("not_needed", 0.99, "can_wait", 0.99)
    envelope["answers"][question]["confidence"] = value
    result = parse(envelope)
    assert result["choice"] == "respond_now" and result["fallback"] == "malformed"


@pytest.mark.parametrize("question", [0, 1])
@pytest.mark.parametrize("change", [{"type": "text"}, {"choice": "batch"}, {"choice": []}])
def test_malformed_question_cannot_be_ignored_even_if_other_question_justifies_suppression(question, change):
    envelope = response("not_needed", 0.99, "can_wait", 0.99)
    envelope["answers"][question].update(change)
    result = parse(envelope)
    assert result["choice"] == "respond_now" and result["fallback"] == "malformed"


@pytest.mark.parametrize("question", [0, 1])
def test_refusal_of_either_required_question_fails_open(question):
    envelope = response("not_needed", 0.99, "can_wait", 0.99)
    envelope["answers"][question] = {"name": envelope["answers"][question]["name"], "type": "refusal"}
    result = parse(envelope)
    assert result["choice"] == "respond_now" and result["fallback"] == "refusal"


@pytest.mark.parametrize("shape", ["missing", "duplicate", "unknown", "extra", "non_object"])
def test_both_named_questions_are_unique_and_complete(shape):
    envelope = response()
    if shape == "missing":
        envelope["answers"].pop()
    elif shape == "duplicate":
        envelope["answers"][1] = copy.deepcopy(envelope["answers"][0])
    elif shape == "unknown":
        envelope["answers"][1]["name"] = "other"
    elif shape == "extra":
        envelope["answers"].append(copy.deepcopy(envelope["answers"][0]))
    else:
        envelope["answers"][1] = None
    result = parse(envelope)
    assert result["choice"] == "respond_now" and result["fallback"] == "malformed"


def test_outgoing_single_question_answer_is_rejected():
    envelope = {"answers": [{"name": "delivery", "type": "choice", "choice": "batch", "confidence": 0.95}]}
    result = parse_answer("outgoing", envelope)
    assert result["choice"] == "send_now" and result["fallback"] == "malformed"


def test_runtime_rechecks_effective_confidence_and_current_policy_without_trusting_raw_answers():
    router = object.__new__(DiplomacyRouting)
    router.settings = SimpleNamespace(
        decisions_routing_policy=POLICY_VERSION,
        decisions_incoming_batch_probability=0.9,
        decisions_incoming_suppress_probability=0.9,
    )
    result = {**parse(response("not_needed", 0.4, "can_wait", 0.95)), "policyVersion": POLICY_VERSION}
    assert router.validate_decision("incoming", result)["choice"] == "batch"
    forged = router.validate_decision("incoming", {**result, "confidence": 0.01})
    assert forged["choice"] == "respond_now" and forged["fallback"] == "low_confidence"
    wrong = router.validate_decision("incoming", {**result, "policyVersion": "diplomacy-routing-v2"})
    assert wrong["choice"] == "respond_now" and wrong["fallback"] == "routing_policy_mismatch"


@pytest.mark.parametrize("policy", [None, "diplomacy-routing-v1", "diplomacy-routing-v2"])
def test_runtime_rejects_missing_or_removed_policy_metadata(policy):
    router = object.__new__(DiplomacyRouting)
    router.settings = SimpleNamespace(
        decisions_routing_policy=POLICY_VERSION,
        decisions_incoming_batch_probability=0.9,
        decisions_incoming_suppress_probability=0.9,
    )
    result = parse(response("not_needed", 0.99, "can_wait", 0.99))
    if policy is not None:
        result["policyVersion"] = policy
    guarded = router.validate_decision("incoming", result)
    assert guarded["choice"] == "respond_now" and guarded["fallback"] == "routing_policy_mismatch"


@pytest.mark.parametrize(
    ("need_confidence", "timing_confidence", "original_choice", "forged_choice"),
    [
        (0.4, 0.95, "batch", "no_separate_response"),
        (0.95, 0.4, "no_separate_response", "batch"),
    ],
)
def test_runtime_cannot_repurpose_one_questions_confidence_for_the_other_action(
    need_confidence, timing_confidence, original_choice, forged_choice
):
    router = object.__new__(DiplomacyRouting)
    router.settings = SimpleNamespace(
        decisions_routing_policy=POLICY_VERSION,
        decisions_incoming_batch_probability=0.9,
        decisions_incoming_suppress_probability=0.9,
    )
    result = {
        **parse(response("not_needed", need_confidence, "can_wait", timing_confidence)),
        "policyVersion": POLICY_VERSION,
    }
    assert result["choice"] == original_choice and result["confidence"] == 0.95
    assert router.validate_decision("incoming", result)["choice"] == original_choice
    forged = router.validate_decision("incoming", {**result, "choice": forged_choice})
    assert forged["choice"] == "respond_now" and forged["fallback"] == "malformed_broker_output"


def test_raw_two_question_metadata_cannot_upgrade_an_effective_immediate_result():
    router = object.__new__(DiplomacyRouting)
    router.settings = SimpleNamespace(
        decisions_routing_policy=POLICY_VERSION,
        decisions_incoming_batch_probability=0.9,
        decisions_incoming_suppress_probability=0.9,
    )
    confident_batch = parse(response("needed", 0.95, "can_wait", 0.95))
    immediate = {
        **confident_batch,
        "policyVersion": POLICY_VERSION,
        "choice": "respond_now",
        "fallback": "routing_deadline",
    }
    guarded = router.validate_decision("incoming", immediate)
    assert guarded["choice"] == "respond_now" and guarded["fallback"] == "routing_deadline"
