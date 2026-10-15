"""Version-pinned advisory replay; live mode explicitly uses the trusted broker."""

import argparse
import asyncio
import json
import math
from collections import Counter
from pathlib import Path
from statistics import median

from civ_agent.advisory_answers import UNAVAILABLE, assemble, outcome
from civ_agent.decisions_broker import RoutingClient
from civ_agent.decisions_policy import DecisionsSettings
from civ_agent.decisions_tasks import TASK_VERSION, request_body, request_metadata, validate_result
from civ_agent.protocol import JsonObject

BANDS = ((0.0, 0.5), (0.5, 0.7), (0.7, 0.8), (0.8, 0.9), (0.9, 1.01))
IDENTITY_FIELDS = ("requestId", "model", "policyVersion", "inputHash", "promptHash", "questionNames")


def load_cases(path):
    fixture = json.loads(Path(path).read_text())
    if fixture.get("version") != 3 or fixture.get("policyVersion") != TASK_VERSION:
        raise ValueError(
            "Replay requires the current versioned advisory fixture; historical captures are not current evidence"
        )
    cases = fixture.get("cases")
    if not isinstance(cases, list) or not cases or len({c["id"] for c in cases}) != len(cases):
        raise ValueError("Replay requires distinct labeled cases")
    groups = {}
    for case in cases:
        if case.get("evidence", {}).get("version") != TASK_VERSION:
            raise ValueError("Replay evidence has a different task version")
        body = request_body(case["evidence"], DecisionsSettings(decisions_enabled=True))
        allowed = {q["name"]: {c["value"] for c in q["choices"]} for q in body["questions"]}
        labels = case.get("labels", {})
        if not case.get("provenance") or set(labels) != set(allowed):
            raise ValueError("Replay requires provenance and a label for every submitted question")
        if any(
            not isinstance(values, list) or not values or not set(values) <= allowed[name]
            for name, values in labels.items()
        ):
            raise ValueError("Replay labels must match fixed task choices")
        group, split = case.get("group"), case.get("split", "unspecified")
        if split not in {"calibration", "holdout", "unspecified"}:
            raise ValueError("Unsupported replay split")
        if group:
            if group in groups and groups[group] != split:
                raise ValueError("Related replay cases cannot cross calibration and holdout")
            groups[group] = split
    return cases


def _task(name):
    prefix = name.split(":", 1)[0]
    return "retrieval" if prefix.startswith("retrieval_") else prefix


def _scores(questions):
    verified = [row for row in questions if row["identityVerified"]]
    raw = [row for row in verified if row["rawChoice"] is not None]
    accepted = [row for row in verified if row["acceptedChoice"] is not None]
    confidences = [row["confidence"] for row in raw]
    bands = []
    for lower, upper in BANDS:
        selected = [row for row in raw if lower <= row["confidence"] < upper]
        bands.append(
            {
                "lower": lower,
                "upper": min(upper, 1),
                "questions": len(selected),
                "rawMatches": sum(row["rawMatches"] for row in selected),
                "accuracy": sum(row["rawMatches"] for row in selected) / len(selected) if selected else None,
            }
        )
    return {
        "fixtureQuestions": len(questions),
        "identityVerifiedQuestions": len(verified),
        "unverifiedQuestions": len(questions) - len(verified),
        "statuses": dict(Counter(row["status"] for row in questions)),
        "rawChoiceQuestions": len(raw),
        "rawMatches": sum(row["rawMatches"] for row in raw),
        "rawErrors": sum(not row["rawMatches"] for row in raw),
        "acceptedQuestions": len(accepted),
        "acceptedMatches": sum(row["acceptedMatches"] for row in accepted),
        "acceptedErrors": sum(not row["acceptedMatches"] for row in accepted),
        "rawChoices": dict(Counter(row["rawChoice"] for row in raw)),
        "acceptedChoices": dict(Counter(row["acceptedChoice"] for row in accepted)),
        "confidence": {
            "known": len(confidences),
            "median": median(confidences) if confidences else None,
            "min": min(confidences) if confidences else None,
            "max": max(confidences) if confidences else None,
            "bands": bands,
        },
        "probabilityMassOnExpected": {
            "known": sum(row["probabilityMassOnExpected"] is not None for row in raw),
            "mean": sum(row["probabilityMassOnExpected"] for row in raw if row["probabilityMassOnExpected"] is not None)
            / sum(row["probabilityMassOnExpected"] is not None for row in raw)
            if any(row["probabilityMassOnExpected"] is not None for row in raw)
            else None,
        },
    }


def _delivery(case, parsed, supplied):
    if supplied is None:
        return {"status": "not_observed", "deliveredNames": [], "staleNames": []}
    if not isinstance(supplied, dict):
        return {"status": "invalid_evidence", "deliveredNames": [], "staleNames": []}
    allowed = set(case["labels"])
    accepted = {answer["name"] for answer in parsed.get("answers", [])}
    result: JsonObject = {}
    for field in ("deliveredNames", "staleNames"):
        names = supplied.get(field, [])
        if not isinstance(names, list) or any(not isinstance(name, str) or name not in allowed for name in names):
            return {"status": "invalid_evidence", "deliveredNames": [], "staleNames": []}
        result[field] = list(dict.fromkeys(names))
    result["status"] = "observed" if set(result["deliveredNames"]) <= accepted else "inconsistent_evidence"
    return result


def report(cases, responses, *, threshold=0.90, deliveries=None):
    if type(threshold) not in (float, int) or not math.isfinite(threshold) or not 0 <= threshold <= 1:
        raise ValueError("Replay confidence threshold must be between zero and one")
    rows, all_questions, usage, fallbacks = [], [], Counter(), Counter()
    for case in cases:
        body = request_body(case["evidence"], DecisionsSettings(decisions_enabled=True))
        expected = request_metadata(body, case["evidence"])
        supplied = responses.get(case["id"])
        verified = isinstance(supplied, dict) and all(supplied.get(key) == expected[key] for key in IDENTITY_FIELDS)
        parsed: JsonObject
        if supplied is None:
            parsed, status = {"answers": []}, "missing_response"
        elif not verified:
            parsed, status = {"answers": []}, "identity_mismatch"
        else:
            parsed, status = validate_result(body, case["evidence"], supplied, case.get("threshold", 0.90)), "evaluated"
            if threshold != case.get("threshold", 0.90) and parsed.get("fallback") != "malformed_broker_output":
                questions_by_name = {question["name"]: question for question in body["questions"]}
                rechecked = [
                    outcome(questions_by_name[row["name"]], row["rawAnswer"], threshold)
                    if "rawAnswer" in row
                    else {"name": row["name"], "threshold": threshold, "status": row["status"]}
                    for row in parsed["questionResults"]
                ]
                unavailable = parsed.get("fallback") in UNAVAILABLE and all(
                    row["status"] == "missing" for row in rechecked
                )
                parsed = {**parsed, **assemble(rechecked, fallback=parsed.get("fallback") if unavailable else None)}
            if parsed.get("fallback"):
                fallbacks[parsed["fallback"]] += 1
            supplied_usage = supplied.get("usage")
            for key, value in (supplied_usage if isinstance(supplied_usage, dict) else {}).items():
                if type(value) is int and value >= 0:
                    usage[key] += value
        accepted = {answer["name"]: answer["choice"] for answer in parsed.get("answers", [])}
        statuses = {answer["name"]: answer for answer in parsed.get("questionResults", [])}
        raw = {}
        for answer in parsed.get("rawAnswers", []):
            raw.setdefault(answer["name"], []).append(answer)
        questions = []
        allowed = {q["name"]: {value["value"] for value in q["choices"]} for q in body["questions"]}
        for name, labels in case["labels"].items():
            originals = raw.get(name, [])
            answer = originals[0] if len(originals) == 1 else {}
            raw_choice = (
                answer.get("choice")
                if answer.get("type") == "choice"
                and answer.get("choice") in allowed[name]
                and statuses.get(name, {}).get("status") != "malformed"
                else None
            )
            confidence = answer.get("confidence") if raw_choice is not None else None
            vector = answer.get("probabilities")
            mass = (
                sum(item["probability"] for item in vector if item["value"] in labels)
                if raw_choice is not None and isinstance(vector, list)
                else None
            )
            question = {
                "case": case["id"],
                "name": name,
                "task": _task(name),
                "questionType": name.split(":", 1)[0],
                "split": case.get("split", "unspecified"),
                "expected": labels,
                "identityVerified": verified,
                "status": statuses.get(name, {}).get("status", "not_observed") if verified else status,
                "rawChoice": raw_choice,
                "acceptedChoice": accepted.get(name),
                "rawMatches": raw_choice in labels if raw_choice is not None else None,
                "acceptedMatches": accepted.get(name) in labels if name in accepted else None,
                "confidence": confidence,
                "probabilityMassOnExpected": mass,
            }
            questions.append(question)
        all_questions.extend(questions)
        rows.append(
            {
                "id": case["id"],
                "group": case.get("group"),
                "split": case.get("split", "unspecified"),
                "status": status,
                "identityVerified": verified,
                "fallback": parsed.get("fallback"),
                "questions": questions,
                "delivery": _delivery(case, parsed, (deliveries or {}).get(case["id"])),
            }
        )
    evaluated = [row for row in rows if row["identityVerified"]]
    totals = _scores(all_questions)
    return {
        "policyVersion": TASK_VERSION,
        "scope": "Frozen labeled advisory replay; accepted advice and observed delivery are separate. This does not measure gameplay strength.",
        "threshold": threshold,
        "cases": rows,
        "completed": sum(row["status"] != "missing_response" for row in rows),
        "missing": sum(row["status"] == "missing_response" for row in rows),
        "identityVerifiedCases": len(evaluated),
        "identityMismatches": sum(row["status"] == "identity_mismatch" for row in rows),
        "labeledAnswers": totals["identityVerifiedQuestions"],
        "labelMatches": totals["acceptedMatches"],
        "rawLabeledAnswers": totals["rawChoiceQuestions"],
        "rawLabelMatches": totals["rawMatches"],
        "questions": totals,
        "byTask": {
            name: _scores([q for q in all_questions if q["task"] == name])
            for name in sorted({q["task"] for q in all_questions})
        },
        "byQuestionType": {
            name: _scores([q for q in all_questions if q["questionType"] == name])
            for name in sorted({q["questionType"] for q in all_questions})
        },
        "bySplit": {
            name: _scores([q for q in all_questions if q["split"] == name])
            for name in sorted({q["split"] for q in all_questions})
        },
        "delivery": {
            "casesWithObservedEvidence": sum(row["delivery"]["status"] == "observed" for row in rows),
            "deliveredQuestions": sum(
                len(row["delivery"]["deliveredNames"]) for row in rows if row["delivery"]["status"] == "observed"
            ),
            "staleQuestions": sum(
                len(row["delivery"]["staleNames"]) for row in rows if row["delivery"]["status"] == "observed"
            ),
        },
        "usage": dict(usage),
        "fallbacks": dict(fallbacks),
        "estimatedValidatedInputCostUsd": usage["input_tokens"] * 0.1 / 1_000_000,
        "responses": responses,
        "limitations": "Labels are provisional assistant judgments. Related scenarios are not independent. Raw-answer calibration excludes refusals, malformed and unavailable answers; acceptance is not delivery, adoption or evidence of gameplay benefit. Historical policy captures cannot be relabeled as current responses.",
    }


def calibrate(cases, responses, *, thresholds=(0.7, 0.8, 0.9, 0.95, 0.99)):
    calibration = [case for case in cases if case.get("split") == "calibration"]
    if not calibration:
        raise ValueError("Calibration requires explicit calibration cases")
    return {
        "selectedThreshold": None,
        "candidates": [
            {
                "threshold": threshold,
                "questions": report(calibration, responses, threshold=threshold)["questions"],
                "byTask": report(calibration, responses, threshold=threshold)["byTask"],
            }
            for threshold in thresholds
        ],
        "holdoutSelectionUsed": False,
        "limitations": "Calibration-only descriptive sweep; no automatic threshold or policy selection.",
    }


async def live(cases, client):
    health = await client.health()
    if health.get("status") != "ready":
        raise ValueError("Authenticated compatible broker required for live replay")
    responses: JsonObject = {}
    for case in cases:
        responses[case["id"]] = await client.batch(case["evidence"])
    return report(cases, responses)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("fixtures", type=Path)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--responses", type=Path)
    mode.add_argument("--live-broker", action="store_true", help="Paid requests charged to the broker-owned allowance")
    parser.add_argument(
        "--calibrate", action="store_true", help="Report calibration-only sweeps; never select a threshold"
    )
    args = parser.parse_args(argv)
    cases = load_cases(args.fixtures)
    responses = json.loads(args.responses.read_text()) if args.responses else {}
    result = (
        asyncio.run(live(cases, RoutingClient.from_environment())) if args.live_broker else report(cases, responses)
    )
    if args.calibrate:
        result["calibration"] = calibrate(cases, result["responses"])
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
