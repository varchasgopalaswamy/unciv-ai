import pytest

from civ_agent.advisory_answers import parse_answers
from civ_agent.diplomacy_decisions import parse_answer, request_body
from scripts.compare_decisions_models import concentration_envelope, envelope, local_answers, local_request


def test_local_adapter_preserves_the_production_prompt_and_descriptions():
    body = request_body("incoming", {"text": "Can you help?"})
    local = local_request(body)
    assert local["state"] == body["input"]
    for question in body["questions"]:
        translated = local["questions"][question["name"]]
        assert translated["instructions"] == question["instructions"]
        assert translated["criteria"] == {c["value"]: c["description"] for c in question["choices"]}


def test_entropy_confidence_is_preserved_instead_of_max_probability():
    response = {
        "answers": {
            "response_needed": {
                "type": "choice",
                "choice": "needed",
                "confidence": 0.6,
                "probabilities": {"needed": 0.92, "not_needed": 0.08},
            }
        }
    }
    adapted = local_answers(response)
    body = request_body("incoming", {})
    parsed = parse_answers(body, adapted, 0.9)
    assert parsed["answers"] == []
    assert parsed["rawAnswers"][0]["confidence"] == 0.6


def test_routing_uses_existing_suppression_and_batch_thresholds():
    response = {
        "answers": {
            "response_needed": {"type": "choice", "choice": "not_needed", "confidence": 0.5},
            "response_timing": {"type": "choice", "choice": "can_wait", "confidence": 0.99},
        }
    }
    result = parse_answer("incoming", local_answers(response))
    assert result["choice"] == "batch"
    assert result["fallback"] == "low_confidence"


@pytest.mark.parametrize("provider", ["local", "luna"])
def test_deadline_view_discards_late_answers_but_diagnostic_view_retains_them(provider):
    answer = {"type": "choice", "choice": "needed", "confidence": 0.99}
    response = {
        "answers": {"response_needed": answer} if provider == "local" else [{"name": "response_needed", **answer}]
    }
    capture = {"response": response, "latencyMs": 5001}
    assert envelope(capture, provider)["answers"]
    assert envelope(capture, provider, deadline=5)["fallback"] == "timeout"
    assert envelope(capture, provider, deadline=5)["answers"] == []


def test_invalid_probability_vector_is_rejected_by_existing_parser():
    response = {
        "answers": {
            "response_needed": {
                "type": "choice",
                "choice": "needed",
                "confidence": 0.99,
                "probabilities": {"needed": 0.9, "not_needed": 0.9},
            }
        }
    }
    body = request_body("incoming", {})
    parsed = parse_answers(body, local_answers(response), 0.9)
    assert parsed["answers"] == []
    assert parsed["questionResults"][0]["status"] == "malformed"


def test_common_concentration_is_derived_without_overwriting_original_confidence():
    response = {
        "answers": {
            "response_needed": {
                "type": "choice",
                "choice": "needed",
                "confidence": 0.6,
                "probabilities": {"needed": 0.92, "not_needed": 0.08},
            }
        }
    }
    capture = {"response": response, "latencyMs": 1}
    derived = concentration_envelope(capture, "local", request_body("incoming", {}))
    assert derived["answers"][0]["confidence"] == pytest.approx(0.84)
    assert response["answers"]["response_needed"]["confidence"] == 0.6


def test_common_concentration_does_not_move_exact_thresholds_due_to_float_roundoff():
    capture = {
        "response": {
            "answers": [
                {
                    "name": "response_needed",
                    "type": "choice",
                    "choice": "needed",
                    "confidence": 0.9,
                    "probabilities": [
                        {"value": "needed", "probability": 0.95},
                        {"value": "not_needed", "probability": 0.05},
                    ],
                }
            ]
        },
        "latencyMs": 1,
    }
    derived = concentration_envelope(capture, "luna", request_body("incoming", {}))
    assert derived["answers"][0]["confidence"] == 0.9
