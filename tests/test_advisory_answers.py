"""Independent question failures and transport diagnostics survive both boundaries."""

import copy

import pytest

from civ_agent.decisions_policy import DecisionsSettings
from civ_agent.decisions_tasks import parse_answers, request_body, request_metadata, validate_result
from civ_agent.protocol import JsonObject


def evidence():
    return {
        "player": "Rome",
        "preservation": [{"key": f"event:{i}", "version": 1} for i in range(4)],
    }


def answer(index, confidence=0.99):
    return {
        "type": "choice",
        "name": f"preservation:{index}",
        "choice": "consider",
        "confidence": confidence,
        "probabilities": [{"value": "consider", "probability": 0.99}, {"value": "skip", "probability": 0.01}],
    }


def test_partial_failures_keep_raw_confidence_and_preserve_true_reasons():
    supplied = evidence()
    body = request_body(supplied, DecisionsSettings())
    parsed = parse_answers(
        body,
        {
            "answers": [
                answer(0),
                answer(1, 0.7),
                {"name": "preservation:2", "type": "refusal", "reason": "private text"},
            ]
        },
        0.9,
    )
    assert [r["status"] for r in parsed["questionResults"]] == ["accepted", "low_confidence", "refusal", "missing"]
    assert parsed["fallback"] == "low_confidence"
    assert [a["name"] for a in parsed["answers"]] == ["preservation:0"]
    assert parsed["rawAnswers"][1]["confidence"] == 0.7
    assert parsed["rawAnswers"][1]["probabilities"] == answer(1)["probabilities"]
    assert parsed["rawAnswers"][2] == {"name": "preservation:2", "type": "refusal"}
    result = {**request_metadata(body, supplied), **parsed}
    assert validate_result(body, supplied, result, 0.9) == result


@pytest.mark.parametrize("bad", ["unknown_choice", "duplicate", "invalid_confidence", "bad_probabilities"])
def test_identifiable_malformed_answer_does_not_discard_valid_sibling(bad):
    body = request_body(evidence(), DecisionsSettings())
    valid, invalid = answer(0), answer(1)
    if bad == "unknown_choice":
        invalid["choice"] = "invented"
    elif bad == "invalid_confidence":
        invalid["confidence"] = True
    elif bad == "bad_probabilities":
        invalid["probabilities"][0]["probability"] = float("nan")
    raw = [valid, invalid] + ([copy.deepcopy(invalid)] if bad == "duplicate" else [])
    parsed = parse_answers(body, {"answers": raw}, 0.9)
    assert parsed["answers"] == [{k: valid[k] for k in ("name", "type", "choice", "confidence")}]
    assert parsed["questionResults"][1]["status"] == "malformed"
    assert parsed["fallback"] == "malformed"
    result = {**request_metadata(body, evidence()), **parsed}
    assert validate_result(body, evidence(), result, 0.9)["answers"] == parsed["answers"]


@pytest.mark.parametrize("raw", [None, {"answers": "invalid"}, {"answers": [None]}, {"answers": [{"name": "unknown"}]}])
def test_unbound_or_unreadable_envelope_rejects_every_answer(raw):
    body = request_body(evidence(), DecisionsSettings())
    parsed = parse_answers(body, raw, 0.9)
    assert parsed["answers"] == [] and parsed["fallback"] == "malformed"
    assert len(parsed["questionResults"]) == 4


@pytest.mark.parametrize("field", ["requestId", "model", "policyVersion", "inputHash", "promptHash", "questionNames"])
def test_delivery_rejects_wrong_request_identity(field):
    supplied = evidence()
    body = request_body(supplied, DecisionsSettings())
    result: JsonObject = {
        **request_metadata(body, supplied),
        **parse_answers(body, {"answers": [answer(i) for i in range(4)]}, 0.9),
    }
    result[field] = "wrong"
    checked = validate_result(body, supplied, result, 0.9)
    assert checked["fallback"] == "malformed_broker_output" and checked["answers"] == []


@pytest.mark.parametrize(
    "mutation", ["forged_acceptance", "false_refusal", "bad_status", "duplicate_row", "changed_threshold"]
)
def test_delivery_revalidates_diagnostics_and_effective_advice(mutation):
    supplied = evidence()
    body = request_body(supplied, DecisionsSettings())
    result: JsonObject = {**request_metadata(body, supplied), **parse_answers(body, {"answers": [answer(0, 0.7)]}, 0.9)}
    if mutation == "forged_acceptance":
        result["answers"] = [answer(0)]
    elif mutation == "false_refusal":
        result["fallback"] = "refusal"
    elif mutation == "bad_status":
        result["questionResults"][0]["status"] = []
    elif mutation == "duplicate_row":
        result["questionResults"][1] = copy.deepcopy(result["questionResults"][0])
    else:
        result["questionResults"][0]["threshold"] = 0.1
    checked = validate_result(body, supplied, result, 0.9)
    assert checked["fallback"] == "malformed_broker_output" and checked["answers"] == []
