"""Independent question failures and transport diagnostics survive both boundaries."""

import copy
from decimal import Decimal

import pytest

from civ_agent.advisory_answers import choice_probabilities, probability, typed_answer
from civ_agent.decisions_policy import DecisionsSettings
from civ_agent.decisions_tasks import parse_answers, request_body, request_metadata, validate_result
from civ_agent.protocol import JsonObject


def evidence():
    return {
        "player": "Rome",
        "preservation": [{"key": f"event:{i}", "version": 1} for i in range(4)],
    }


def answer(index, confidence=0.99) -> JsonObject:
    return {
        "type": "choice",
        "name": f"preservation:{index}",
        "choice": "consider",
        "confidence": confidence,
        "probabilities": [{"value": "consider", "probability": 0.99}, {"value": "skip", "probability": 0.01}],
    }


@pytest.mark.parametrize("value", [True, False, "0.99", None, -0.1, 1.1, float("nan"), float("inf"), Decimal("0.99")])
@pytest.mark.parametrize("field", ["confidence", "probability", "providerConfidence", "vector_probability"])
def test_answer_schema_rejects_non_json_and_invalid_probabilities_without_coercion(value, field):
    question = request_body(evidence(), DecisionsSettings())["questions"][0]
    supplied = answer(0)
    if field == "vector_probability":
        supplied["probabilities"][0]["probability"] = value
    else:
        supplied[field] = value
    assert typed_answer(question, supplied) is None
    assert not probability(value)


@pytest.mark.parametrize("value", [0, 1, 0.0, 1.0, 0.99])
def test_answer_schema_preserves_integer_and_float_probability_types(value):
    question = request_body(evidence(), DecisionsSettings())["questions"][0]
    supplied = answer(0, value)
    supplied.update(probability=value, providerConfidence=value, providerText="private prose")
    result = typed_answer(question, supplied)
    assert result is not None
    for field in ("confidence", "probability", "providerConfidence"):
        assert result[field] == value and type(result[field]) is type(value)
    assert "providerText" not in result
    assert probability(value)


def test_answer_schema_preserves_absence_and_redacts_all_refusal_diagnostics():
    question = request_body(evidence(), DecisionsSettings())["questions"][0]
    supplied = answer(0)
    del supplied["probabilities"]
    assert typed_answer(question, supplied) == supplied
    assert typed_answer(question, {**supplied, "probabilities": None}) is None
    assert typed_answer(question, {**supplied, "name": "different-question"}) is None
    assert typed_answer(
        question,
        {
            "name": question["name"],
            "type": "refusal",
            "reason": "private refusal prose",
            "confidence": "malformed diagnostic",
            "probabilities": None,
        },
    ) == {"name": question["name"], "type": "refusal"}


def test_shared_vector_schema_retains_distinct_advisory_and_routing_policies():
    question = request_body(evidence(), DecisionsSettings())["questions"][0]
    values = [
        {"value": "skip", "probability": 0.51, "providerText": "private prose"},
        {"value": "consider", "probability": 0.505},
    ]
    # Routing retains the supplied order and permits the existing 0.02 rounding
    # tolerance. Advisory answers require 0.01 and order by the offered choices.
    assert choice_probabilities(["consider", "skip"], values, tolerance=0.02) == [
        {"value": "skip", "probability": 0.51},
        {"value": "consider", "probability": 0.505},
    ]
    assert typed_answer(question, {**answer(0), "probabilities": values}) is None
    values[0]["probability"] = 0.495
    parsed = typed_answer(question, {**answer(0), "probabilities": values})
    assert parsed is not None
    assert parsed["probabilities"] == [
        {"value": "consider", "probability": 0.505},
        {"value": "skip", "probability": 0.495},
    ]


@pytest.mark.parametrize("failure", ["duplicate", "missing", "unknown", "not_list", "not_row"])
def test_shared_vector_schema_rejects_incomplete_or_unbound_distributions(failure):
    values = answer(0)["probabilities"]
    if failure == "duplicate":
        values[1]["value"] = values[0]["value"]
    elif failure == "missing":
        values.pop()
    elif failure == "unknown":
        values[1]["value"] = "unoffered"
    elif failure == "not_list":
        values = {"consider": 0.99, "skip": 0.01}
    else:
        values[1] = "not a probability row"
    assert choice_probabilities(["consider", "skip"], values, tolerance=0.02) is None


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
