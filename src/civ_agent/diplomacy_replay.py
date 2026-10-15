"""Labeled, scheduling-only evaluation. Network use requires --live-broker."""

import argparse
import asyncio
import json
import math
from collections import Counter
from pathlib import Path
from statistics import median

from civ_agent.decisions_broker import RoutingClient
from civ_agent.diplomacy_decisions import (
    CHOICES,
    CONFIDENCE_THRESHOLD,
    POLICY_VERSION,
    QUESTION_CHOICES,
    fallback,
    parse_answer,
)
from civ_agent.protocol import JsonObject

CONFIDENCE_BANDS = ((0.0, 0.5), (0.5, 0.7), (0.7, 0.8), (0.8, 0.9), (0.9, 1.01))


def load_fixture(path):
    fixture = json.loads(Path(path).read_text())
    if fixture.get("version") not in {1, 2} or not isinstance(fixture.get("cases"), list):
        raise ValueError("Unsupported routing fixture format")
    ids, groups = set(), {}
    for case in fixture["cases"]:
        if case["id"] in ids:
            raise ValueError("Duplicate routing case ID")
        ids.add(case["id"])
        direction = case["direction"]
        expected = case["expected"]
        if direction not in CHOICES or not expected or any(choice not in CHOICES[direction] for choice in expected):
            raise ValueError("Invalid routing case labels")
        split, group = case.get("split", "unspecified"), case.get("group")
        if split not in {"calibration", "holdout", "unspecified"}:
            raise ValueError("Invalid routing case split")
        if group:
            if group in groups and groups[group] != split:
                raise ValueError("Routing case group crosses calibration and holdout splits")
            groups[group] = split
    return fixture


def _source(case):
    source = case.get("source", {})
    if source.get("transformation"):
        return "counterfactual"
    return "synthetic" if source.get("synthetic") else "historical" if source.get("artifact") else "unspecified"


def _rate(errors, trials):
    if not trials:
        return {"errors": errors, "decisions": trials, "rate": None, "wilson95": None}
    rate, z = errors / trials, 1.959963984540054
    center = rate + z * z / (2 * trials)
    width = z * math.sqrt(rate * (1 - rate) / trials + z * z / (4 * trials * trials))
    scale = 1 + z * z / trials
    return {
        "errors": errors,
        "decisions": trials,
        "rate": rate,
        "wilson95": [max(0.0, (center - width) / scale), min(1.0, (center + width) / scale)],
    }


def _confidence_summary(values):
    return {
        "reported": len(values),
        "median": median(values) if values else None,
        "min": min(values) if values else None,
        "max": max(values) if values else None,
        "bands": [
            {"lower": lower, "upper": min(upper, 1.0), "calls": sum(lower <= value < upper for value in values)}
            for lower, upper in CONFIDENCE_BANDS
        ],
    }


def _question_summary(rows):
    selected = [row for row in rows if row["policyVersion"] == POLICY_VERSION]
    questions = {}
    for name, choices in {
        name: choices for questions in QUESTION_CHOICES.values() for name, choices in questions.items()
    }.items():
        eligible = [row for row in selected if name in QUESTION_CHOICES[row["direction"]]]
        answers = [
            answer for row in eligible for answer in row["decision"].get("rawAnswers", []) if answer.get("name") == name
        ]
        chosen = [answer for answer in answers if answer.get("type") == "choice"]
        confidences = [answer["confidence"] for answer in chosen]
        probabilities = {
            choice: [
                item["probability"]
                for answer in chosen
                for item in answer.get("probabilities", [])
                if item["value"] == choice
            ]
            for choice in choices
        }
        refused = sum(answer.get("type") == "refusal" for answer in answers)
        questions[name] = {
            "choices": dict(Counter(answer["choice"] for answer in chosen)),
            "choiceResponses": len(chosen),
            "refusals": refused,
            "unknown": len(eligible) - len(chosen) - refused,
            "confidence": _confidence_summary(confidences),
            "probabilities": {
                choice: {"reported": len(values), "median": median(values) if values else None}
                for choice, values in probabilities.items()
            },
        }
    return {
        "calls": len(selected),
        "questions": questions,
        "basis": "Original named binary answers only; unknown includes missing/malformed answers and provider failures. No three-way probability vector is synthesized.",
    }


def _summary(rows):
    scored = [row for row in rows if row["evaluated"]]
    classified = [row for row in scored if not row["protected"]]
    confidence = [row for row in classified if row["decision"].get("confidence") is not None]
    usage: dict[str, float | int] = {}
    for row in classified:
        for key, value in row["usage"].items():
            if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value):
                usage[key] = usage.get(key, 0) + value
    bands = []
    for lower, upper in CONFIDENCE_BANDS:
        selected = [row for row in confidence if lower <= row["decision"]["confidence"] < upper]
        bands.append(
            {
                "lower": lower,
                "upper": min(upper, 1.0),
                "calls": len(selected),
                "rawAccepted": sum(row["rawAccepted"] is True for row in selected),
                "effectiveAccepted": sum(row["accepted"] for row in selected),
            }
        )
    confusion, raw_confusion = Counter(), Counter()
    for row in scored:
        confusion[(row["direction"], "|".join(sorted(row["expected"])), row["decision"]["choice"])] += 1
        if row["rawAvailable"]:
            raw_confusion[(row["direction"], "|".join(sorted(row["expected"])), row["decision"]["rawChoice"])] += 1
    latencies = [row["latencyMs"] for row in classified if row["latencyMs"] is not None]
    batched = [row for row in classified if row["decision"]["choice"] == "batch"]
    omitted = [row for row in classified if row["decision"]["choice"] in {"omit", "no_separate_response"}]
    groups = {}
    for row in classified:
        groups.setdefault(row["group"] or row["id"], []).append(row)
    return {
        "total": len(rows),
        "evaluated": len(scored),
        "missing": len(rows) - len(scored),
        "coverage": len(scored) / len(rows) if rows else None,
        "calls": len(classified),
        "protectedBypasses": sum(row["protected"] for row in scored),
        "accepted": sum(row["accepted"] for row in scored),
        "rawAccepted": sum(row["rawAccepted"] is True for row in classified),
        "rawAvailable": sum(row["rawAvailable"] for row in classified),
        "groupSummary": {
            "groups": len(groups),
            "meanAcceptedFraction": sum(sum(row["accepted"] for row in group) / len(group) for group in groups.values())
            / len(groups)
            if groups
            else None,
            "groupsWithHarmfulDeferral": sum(any(row["harmfulDeferred"] for row in group) for group in groups.values()),
            "groupsWithHarmfulOmission": sum(any(row["harmfulOmitted"] for row in group) for group in groups.values()),
        },
        "actionableDeferred": sum(row["actionableDeferred"] for row in scored),
        "harmfulDeferred": sum(row["harmfulDeferred"] for row in scored),
        "harmfulOmitted": sum(row["harmfulOmitted"] for row in scored),
        "unnecessaryImmediate": sum(row["unnecessaryImmediate"] for row in scored),
        "lowConfidence": sum(row["decision"].get("lowConfidence", False) for row in classified),
        "overridden": sum(row["decision"].get("overridden", False) for row in classified),
        "fallbacks": dict(
            Counter(row["decision"]["fallback"] for row in classified if row["decision"].get("fallback"))
        ),
        "dispositions": dict(Counter(row["decision"]["choice"] for row in scored)),
        "confusion": [
            {"direction": direction, "expected": expected.split("|"), "actual": actual, "count": count}
            for (direction, expected, actual), count in sorted(confusion.items())
        ],
        "rawConfusion": [
            {"direction": direction, "expected": expected.split("|"), "actual": actual, "count": count}
            for (direction, expected, actual), count in sorted(raw_confusion.items())
        ],
        "routingErrorRates": {
            "harmfulBatch": _rate(sum(row["harmfulDeferred"] for row in batched), len(batched)),
            "harmfulOmission": _rate(sum(row["harmfulOmitted"] for row in omitted), len(omitted)),
        },
        "confidenceBands": bands,
        "confidenceBasis": "Confidence of the effective action's governing binary question; normalized margin, not calibrated accuracy.",
        "rawChoiceConfidence": _confidence_summary(
            [row["decision"].get("rawChoiceConfidence", row["decision"]["confidence"]) for row in confidence]
        ),
        "questionSummary": _question_summary(classified),
        "usage": dict(usage),
        "latencyMs": {
            "reported": len(latencies),
            "sum": sum(latencies),
            "mean": sum(latencies) / len(latencies) if latencies else None,
        },
    }


def evaluate(cases, responses, *, threshold=CONFIDENCE_THRESHOLD, thresholds=None):
    """Score provider envelopes, preserving ambiguity and never inventing discarded raw choices."""
    rows: list[JsonObject] = []
    for case in cases:
        response = responses.get(case["id"])
        protected = bool(case.get("protected"))
        row = {
            "id": case["id"],
            "group": case.get("group"),
            "label": case.get("label"),
            "labels": case.get("labels", [case["label"]] if case.get("label") else []),
            "split": case.get("split", "unspecified"),
            "sourceKind": _source(case),
            "direction": case["direction"],
            "expected": case["expected"],
            "protected": protected,
            "policyVersion": response.get("policyVersion") if isinstance(response, dict) else None,
        }
        envelope = response if isinstance(response, dict) else {}
        unavailable = response is None or envelope.get("rawUnavailable", False)
        if protected:
            decision = fallback(case["direction"], "protected")
        elif unavailable:
            rows.append({**row, "evaluated": False, "reason": "raw_response_unavailable"})
            continue
        elif envelope.get("policyVersion") != POLICY_VERSION:
            rows.append({**row, "evaluated": False, "reason": "policy_version_mismatch"})
            continue
        elif envelope.get("fallback"):
            decision = fallback(case["direction"], envelope["fallback"])
        else:
            decision = parse_answer(
                case["direction"],
                response,
                threshold,
                thresholds=thresholds,
                policy_version=POLICY_VERSION,
            )
        immediate = next(iter(CHOICES[case["direction"]]))
        omit = "no_separate_response" if case["direction"] == "incoming" else "omit"
        raw_choice = decision.get("rawChoice")
        latency = envelope.get("latencyMs")
        usage = envelope.get("usage")
        rows.append(
            {
                **row,
                "evaluated": True,
                "decision": decision,
                "accepted": decision["choice"] in case["expected"],
                "rawAccepted": raw_choice in case["expected"] if raw_choice is not None else None,
                "rawAvailable": raw_choice is not None,
                "actionableDeferred": case["expected"] == [immediate] and decision["choice"] != immediate,
                "harmfulDeferred": immediate in case["expected"]
                and "batch" not in case["expected"]
                and decision["choice"] == "batch",
                "harmfulOmitted": omit not in case["expected"] and decision["choice"] == omit,
                "unnecessaryImmediate": immediate not in case["expected"] and decision["choice"] == immediate,
                "usage": usage if isinstance(usage, dict) else {},
                "latencyMs": latency
                if isinstance(latency, (int, float))
                and not isinstance(latency, bool)
                and math.isfinite(latency)
                and latency >= 0
                else None,
            }
        )
    return {
        "cases": rows,
        **_summary(rows),
        "byDirection": {
            direction: _summary([row for row in rows if row["direction"] == direction]) for direction in CHOICES
        },
        "bySplit": {
            split: _summary([row for row in rows if row["split"] == split])
            for split in sorted({row["split"] for row in rows})
        },
        "bySource": {
            kind: _summary([row for row in rows if row["sourceKind"] == kind])
            for kind in sorted({row["sourceKind"] for row in rows})
        },
        "byLabel": {
            label: _summary([row for row in rows if label in row["labels"]])
            for label in sorted({label for row in rows for label in row["labels"]})
        },
        "limitations": "Scheduling labels, including explicit ambiguity; synthetic/counterfactual cases are reported separately. Cases within a scenario, thread, or game can be correlated. Wilson intervals describe this case sample under an independence assumption, not production error bounds; groupSummary weights each group equally. Confidence is not calibrated accuracy. This replay does not estimate gameplay savings or adjudicate promises or truth.",
    }


def calibrate(cases, responses, *, batch_thresholds=(0.7, 0.8, 0.9), suppression_thresholds=(0.9, 0.95, 0.99)):
    """Report candidates on calibration only; leave held-out labels out of threshold selection."""
    calibration = [case for case in cases if case.get("split") == "calibration"]
    if not calibration:
        raise ValueError("Threshold sweeps require an explicit calibration split")
    rows = []
    for batch in batch_thresholds:
        for suppression in suppression_thresholds:
            thresholds = {"batch": batch, "no_separate_response": suppression, "omit": suppression}
            report = evaluate(calibration, responses, thresholds=thresholds)
            rows.append(
                {
                    "thresholds": thresholds,
                    "evaluation": {key: value for key, value in report.items() if key != "cases"},
                }
            )
    return {
        "calibrationCases": len(calibration),
        "candidates": rows,
        "selectedThresholds": None,
        "holdoutAtExistingThreshold": evaluate([case for case in cases if case.get("split") == "holdout"], responses),
        "selectionPolicy": "No automatic threshold selection. Review calibration error evidence, fix the policy, then evaluate holdout once.",
    }


async def collect(cases, client):
    responses = {}
    for case in cases:
        if case.get("protected"):
            continue
        result = await client.classify(case["direction"], case["bundle"])
        if result.get("policyVersion") != POLICY_VERSION:
            raise ValueError("Replay requires the current routing policy")
        answers = result.get("rawAnswers")
        has_raw = isinstance(answers, list)
        responses[case["id"]] = {
            "answers": answers if has_raw else [],
            "rawUnavailable": not has_raw and result.get("fallback") in {None, "low_confidence"},
            "fallback": result.get("fallback") if not has_raw else None,
            "policyVersion": POLICY_VERSION,
            "usage": result.get("usage", {}),
            "latencyMs": result.get("latencyMs"),
            "routingResult": result,
        }
    return responses


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("fixtures", type=Path)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--responses", type=Path, help="Replay original Decisions response envelopes by case id; no network"
    )
    mode.add_argument(
        "--live-broker", action="store_true", help="Explicitly make paid requests through the configured trusted broker"
    )
    parser.add_argument(
        "--calibrate", action="store_true", help="Report calibration-only sweeps; never automatically select a policy"
    )
    parser.add_argument("--batch-thresholds", default="0.7,0.8,0.9", help="Comma-separated calibration candidates")
    parser.add_argument(
        "--suppression-thresholds", default="0.9,0.95,0.99", help="Comma-separated calibration candidates"
    )
    args = parser.parse_args()
    fixture = load_fixture(args.fixtures)
    responses = json.loads(args.responses.read_text()) if args.responses else {}
    if args.live_broker:
        responses = asyncio.run(collect(fixture["cases"], RoutingClient.from_environment()))
    result = {"evaluation": evaluate(fixture["cases"], responses), "responses": responses}
    if args.calibrate:
        try:
            batch = tuple(float(value) for value in args.batch_thresholds.split(","))
            suppression = tuple(float(value) for value in args.suppression_thresholds.split(","))
            if any(not math.isfinite(value) or not 0 <= value <= 1 for value in (*batch, *suppression)):
                raise ValueError("Calibration thresholds must be between zero and one")
            result["calibration"] = calibrate(
                fixture["cases"], responses, batch_thresholds=batch, suppression_thresholds=suppression
            )
        except ValueError as error:
            parser.error(str(error))
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
