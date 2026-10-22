import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from civ_agent.diplomacy_decisions import POLICY_VERSION
from civ_agent.diplomacy_replay import collect
from civ_agent.diplomacy_replay import evaluate as evaluate_default


def evaluate(*args, **kwargs):
    return evaluate_default(*args, **{"threshold": 0.9, **kwargs})


def envelope(direction, choice, confidence=0.99):
    if direction == "incoming":
        answers = [
            {
                "name": "response_needed",
                "type": "choice",
                "choice": "not_needed" if choice == "no_separate_response" else "needed",
                "confidence": confidence,
            },
            {
                "name": "response_timing",
                "type": "choice",
                "choice": "now" if choice == "respond_now" else "can_wait",
                "confidence": confidence,
            },
        ]
    else:
        answers = [
            {
                "name": "delivery_needed",
                "type": "choice",
                "choice": "not_needed" if choice == "omit" else "needed",
                "confidence": confidence,
            },
            {
                "name": "delivery_timing",
                "type": "choice",
                "choice": "now" if choice == "send_now" else "can_wait",
                "confidence": confidence,
            },
        ]
    return {"answers": answers, "policyVersion": POLICY_VERSION}


def cases():
    return json.loads((Path(__file__).parent / "fixtures/diplomacy-routing-replay.json").read_text())["cases"]


def test_labeled_replay_covers_audit_cases_without_future_or_other_audience():
    fixture = cases()
    assert len({case["id"] for case in fixture}) == len(fixture)
    assert {
        "acknowledgement",
        "unchanged_report",
        "answered_question",
        "useful_new_information",
        "changed_terms",
        "urgent_request",
        "bluff",
        "ambiguous_promise",
    } <= {case["label"] for case in fixture}
    for case in fixture:
        if not case["source"].get("artifact") or case["direction"] != "incoming":
            continue
        selected = case["bundle"]["pending"][0]
        for message in case["bundle"]["recentExchanges"]:
            assert message["participants"] == selected["participants"]
            assert message["turn"] <= selected["turn"] and message["message"] != selected["message"]
    responses = {case["id"]: envelope(case["direction"], case["expected"][0], 0.99) for case in fixture}
    scored = evaluate(fixture, responses)
    assert scored["accepted"] == len(fixture) and scored["actionableDeferred"] == 0
    # This tests the replay harness with oracle responses, not the live model.
    responses["mcygkx"] = envelope("incoming", "no_separate_response")
    assert evaluate(fixture, responses)["actionableDeferred"] == 1
    assert evaluate(fixture, {})["missing"] == len(fixture) - 1


def test_replay_never_sends_protected_traffic_to_broker():
    async def run():
        client = AsyncMock()
        client.classify.return_value = {"choice": "respond_now", "confidence": 1, "policyVersion": POLICY_VERSION}
        result = await collect(cases(), client)
        assert "mpvwks" not in result
        assert client.classify.await_count == len(cases()) - 1

    asyncio.run(run())


def test_collect_preserves_original_low_confidence_choice_and_probabilities():
    async def run():
        raw = envelope("incoming", "batch", 0.72)["answers"]
        raw[1]["probabilities"] = [
            {"value": "now", "probability": 0.28},
            {"value": "can_wait", "probability": 0.72},
        ]
        client = AsyncMock()
        client.classify.return_value = {
            "choice": "respond_now",
            "confidence": 0.72,
            "fallback": "low_confidence",
            "rawAnswers": raw,
            "policyVersion": POLICY_VERSION,
            "usage": {"input_tokens": 50},
            "latencyMs": 21,
        }
        result = await collect([cases()[1]], client)
        assert result[cases()[1]["id"]]["answers"] == raw
        assert result[cases()[1]["id"]]["fallback"] is None
        return result

    captured = asyncio.run(run())
    case = {**cases()[1], "expected": ["batch"]}
    assert evaluate([case], captured)["unnecessaryImmediate"] == 1
    lower = evaluate([case], captured, thresholds={"batch": 0.7})
    assert lower["accepted"] == 1 and lower["rawAccepted"] == 1


def test_collect_marks_missing_original_answers_unavailable_instead_of_fabricating_them():
    async def run():
        client = AsyncMock()
        client.classify.return_value = {
            "choice": "respond_now",
            "confidence": 0.67,
            "fallback": "low_confidence",
            "policyVersion": POLICY_VERSION,
        }
        return await collect([cases()[1]], client)

    captured = asyncio.run(run())
    assert captured[cases()[1]["id"]]["rawUnavailable"] is True
    assert captured[cases()[1]["id"]]["answers"] == []
    assert evaluate([cases()[1]], captured)["missing"] == 1


@pytest.mark.parametrize("policy", [None, "diplomacy-routing-v1", "diplomacy-routing-v2"])
def test_replay_requires_explicit_current_policy_for_scoring_and_collection(policy):
    case = cases()[1]
    response = {**envelope("incoming", "batch"), "policyVersion": policy}
    scored = evaluate([case], {case["id"]: response})
    assert scored["evaluated"] == 0
    assert scored["cases"][0]["reason"] == "policy_version_mismatch"

    async def run():
        client = AsyncMock()
        client.classify.return_value = {"rawAnswers": response["answers"], "policyVersion": policy}
        with pytest.raises(ValueError, match="current routing policy"):
            await collect([case], client)

    asyncio.run(run())


def test_replay_reports_ambiguous_labels_errors_usage_and_confidence_separately():
    fixture = [
        {"id": "urgent", "label": "urgent", "direction": "incoming", "expected": ["respond_now"], "split": "holdout"},
        {
            "id": "flexible",
            "labels": ["new", "ordinary"],
            "direction": "outgoing",
            "expected": ["send_now", "batch"],
            "split": "calibration",
        },
        {
            "id": "duplicate",
            "label": "redundant",
            "direction": "incoming",
            "expected": ["no_separate_response"],
            "split": "calibration",
        },
    ]
    responses = {
        "urgent": {
            **envelope("incoming", "batch"),
            "usage": {"input_tokens": 40},
            "latencyMs": 10,
        },
        "flexible": {
            **envelope("outgoing", "omit", 0.97),
            "usage": {"input_tokens": 30},
            "latencyMs": 20,
        },
        "duplicate": envelope("incoming", "respond_now", 0.6),
    }
    report = evaluate(fixture, responses)
    assert report["harmfulDeferred"] == report["harmfulOmitted"] == report["unnecessaryImmediate"] == 1
    assert report["usage"] == {"input_tokens": 70}
    assert report["latencyMs"] == {"reported": 2, "sum": 30, "mean": 15}
    assert report["bySplit"]["holdout"]["evaluated"] == 1
    assert report["byDirection"]["outgoing"]["harmfulOmitted"] == 1
    assert report["confidenceBands"][1]["calls"] == 1
    assert report["confidenceBands"][-1]["calls"] == 2


def test_threshold_sweep_uses_only_calibration_cases_and_never_selects_automatically():
    from civ_agent.diplomacy_replay import calibrate

    fixture = [
        {"id": "cal", "label": "normal", "direction": "incoming", "expected": ["batch"], "split": "calibration"},
        {"id": "held", "label": "urgent", "direction": "incoming", "expected": ["respond_now"], "split": "holdout"},
    ]
    responses = {case["id"]: envelope("incoming", "batch", 0.8) for case in fixture}
    report = calibrate(fixture, responses, batch_thresholds=(0.7,), suppression_thresholds=(0.99,))
    assert report["selectedThresholds"] is None
    assert report["candidates"][0]["evaluation"]["accepted"] == 1
    assert report["candidates"][0]["evaluation"]["total"] == 1
    assert report["holdoutAtExistingThreshold"]["accepted"] == 1
    assert report["holdoutAtExistingThreshold"]["cases"][0]["decision"]["choice"] == "respond_now"


def test_fixture_loader_rejects_group_leakage(tmp_path):
    import pytest

    from civ_agent.diplomacy_replay import load_fixture

    path = tmp_path / "fixture.json"
    path.write_text(
        json.dumps(
            {
                "version": 2,
                "cases": [
                    {**cases()[1], "id": split, "split": split, "group": "shared-thread"}
                    for split in ("calibration", "holdout")
                ],
            }
        )
    )
    with pytest.raises(ValueError, match="crosses"):
        load_fixture(path)


def test_repeated_scenario_variants_do_not_dominate_group_accuracy():
    fixture = [
        {"id": str(index), "group": group, "direction": "incoming", "expected": ["batch"]}
        for index, group in enumerate(["many-variants"] * 9 + ["one-case"])
    ]
    responses = {
        case["id"]: envelope("incoming", "batch" if case["group"] == "many-variants" else "no_separate_response")
        for case in fixture
    }
    report = evaluate(fixture, responses)
    assert report["accepted"] == 9
    assert report["groupSummary"] == {
        "groups": 2,
        "meanAcceptedFraction": 0.5,
        "groupsWithHarmfulDeferral": 0,
        "groupsWithHarmfulOmission": 1,
    }


def test_two_question_collection_preserves_both_original_answers_and_policy():
    raw = [
        {
            "name": "response_needed",
            "type": "choice",
            "choice": "needed",
            "confidence": 0.96,
            "probabilities": [{"value": "needed", "probability": 0.97}, {"value": "not_needed", "probability": 0.03}],
        },
        {
            "name": "response_timing",
            "type": "choice",
            "choice": "can_wait",
            "confidence": 0.95,
            "probabilities": [{"value": "now", "probability": 0.04}, {"value": "can_wait", "probability": 0.96}],
        },
    ]

    async def run():
        client = AsyncMock()
        client.classify.return_value = {
            "choice": "batch",
            "confidence": 0.95,
            "fallback": None,
            "rawChoice": "batch",
            "rawAnswers": raw,
            "policyVersion": POLICY_VERSION,
        }
        return await collect([cases()[1]], client)

    captured = asyncio.run(run())
    envelope = captured[cases()[1]["id"]]
    assert envelope["answers"] == raw and envelope["policyVersion"] == POLICY_VERSION
    assert "rawAnswer" not in envelope and "probabilities" not in envelope
    report = evaluate([{**cases()[1], "expected": ["batch"]}], captured)
    assert report["rawAccepted"] == report["accepted"] == 1
    assert report["cases"][0]["decision"]["rawAnswers"] == raw
    assert report["cases"][0]["decision"].get("rawAnswer") is None


def test_two_question_replay_rejects_missing_duplicate_and_old_single_delivery_answers():
    needed = {"name": "response_needed", "type": "choice", "choice": "needed", "confidence": 0.99}
    wrong = {"name": "delivery", "type": "choice", "choice": "batch", "confidence": 0.99}
    for answers in ([needed], [needed, needed], [wrong]):
        result = evaluate([cases()[1]], {cases()[1]["id"]: {"answers": answers, "policyVersion": POLICY_VERSION}})
        decision = result["cases"][0]["decision"]
        assert decision["choice"] == "respond_now" and decision["fallback"] == "malformed"
        assert result["rawAvailable"] == 0


def test_two_question_low_confidence_suppression_can_fall_back_to_supported_batch():
    answers = [
        {"name": "response_needed", "type": "choice", "choice": "not_needed", "confidence": 0.4},
        {"name": "response_timing", "type": "choice", "choice": "can_wait", "confidence": 0.98},
    ]
    case = {**cases()[1], "expected": ["batch"]}
    report = evaluate([case], {case["id"]: {"answers": answers, "policyVersion": POLICY_VERSION}})
    decision = report["cases"][0]["decision"]
    assert decision["choice"] == "batch" and decision["rawChoice"] == "no_separate_response"
    assert decision["confidence"] == 0.98 and decision["rawChoiceConfidence"] == 0.4
    assert report["overridden"] == 1 and report["dispositions"] == {"batch": 1}
    assert report["harmfulOmitted"] == 0 and report["accepted"] == 1


def test_two_question_summary_separates_confidence_probabilities_refusals_and_missing():
    case = cases()[1]
    fixture = [{**case, "id": name, "expected": ["batch", "respond_now"]} for name in ("valid", "refused", "timeout")]
    responses = {
        "valid": {
            "policyVersion": POLICY_VERSION,
            "answers": [
                {
                    "name": "response_needed",
                    "type": "choice",
                    "choice": "needed",
                    "confidence": 0.8,
                    "probabilities": [
                        {"value": "needed", "probability": 0.9},
                        {"value": "not_needed", "probability": 0.1},
                    ],
                },
                {
                    "name": "response_timing",
                    "type": "choice",
                    "choice": "can_wait",
                    "confidence": 0.96,
                    "probabilities": [
                        {"value": "now", "probability": 0.02},
                        {"value": "can_wait", "probability": 0.98},
                    ],
                },
            ],
        },
        "refused": {
            "policyVersion": POLICY_VERSION,
            "answers": [
                {"name": "response_needed", "type": "refusal"},
                {"name": "response_timing", "type": "choice", "choice": "now", "confidence": 0.99},
            ],
        },
        "timeout": {"policyVersion": POLICY_VERSION, "answers": [], "fallback": "timeout"},
    }
    report = evaluate(fixture, responses)
    questions = report["questionSummary"]
    assert questions["calls"] == 3
    needed = questions["questions"]["response_needed"]
    assert needed["choices"] == {"needed": 1}
    assert needed["refusals"] == needed["unknown"] == 1
    assert needed["confidence"]["median"] == 0.8
    assert needed["probabilities"]["needed"] == {"reported": 1, "median": 0.9}
    timing = questions["questions"]["response_timing"]
    assert timing["choices"] == {"can_wait": 1, "now": 1}
    assert timing["refusals"] == 0 and timing["unknown"] == 1
    assert timing["confidence"]["median"] == 0.975
    assert timing["probabilities"]["can_wait"] == {"reported": 1, "median": 0.98}
    assert report["confidenceBands"][-1]["calls"] == 1
