"""Typed per-question advisory outcomes, independent of delivery or accounting."""

import math
from collections import defaultdict

from civ_agent.protocol import JsonObject

STATUSES = {"accepted", "low_confidence", "refusal", "malformed", "missing"}
UNAVAILABLE = {"budget_exhausted", "completion_unknown", "timeout", "service_failure", "circuit_open", "malformed"}


def probability(value):
    return type(value) in (int, float) and math.isfinite(value) and 0 <= value <= 1


def typed_answer(question, answer):
    """Retain only the API's typed fields, never refusal/provider prose."""
    name = question["name"]
    if not isinstance(answer, dict) or answer.get("name") != name:
        return None
    if answer.get("type") == "refusal":
        return {"name": name, "type": "refusal"}
    choices = [c["value"] for c in question["choices"]]
    if (
        answer.get("type") != "choice"
        or not isinstance(answer.get("choice"), str)
        or answer["choice"] not in choices
        or not probability(answer.get("confidence"))
    ):
        return None
    raw = {k: answer[k] for k in ("name", "type", "choice", "confidence")}
    if "probabilities" in answer:
        values = answer["probabilities"]
        if (
            not isinstance(values, list)
            or len(values) != len(choices)
            or any(
                not isinstance(v, dict)
                or not isinstance(v.get("value"), str)
                or v["value"] not in choices
                or not probability(v.get("probability"))
                for v in values
            )
            or len({v["value"] for v in values}) != len(choices)
            or not math.isclose(sum(v["probability"] for v in values), 1, abs_tol=0.01)
        ):
            return None
        lookup = {v["value"]: v["probability"] for v in values}
        raw["probabilities"] = [{"value": c, "probability": lookup[c]} for c in choices]
    return raw


def outcome(question, answer, threshold):
    raw = typed_answer(question, answer)
    row = {"name": question["name"], "threshold": threshold}
    if raw is None:
        return {**row, "status": "malformed"}
    status = (
        "refusal" if raw["type"] == "refusal" else "accepted" if raw["confidence"] >= threshold else "low_confidence"
    )
    return {**row, "status": status, "rawAnswer": raw}


def assemble(rows, *, fallback=None):
    accepted, raw = [], []
    for row in rows:
        answer = row.get("rawAnswer")
        if answer is not None:
            raw.append(answer)
        if row["status"] == "accepted":
            accepted.append({k: answer[k] for k in ("name", "type", "choice", "confidence")})
    return {
        "answers": accepted,
        "rawAnswers": raw,
        "questionResults": rows,
        "fallback": fallback or next((r["status"] for r in rows if r["status"] != "accepted"), None),
    }


def failed_answers(body, threshold, reason) -> JsonObject:
    return assemble(
        [{"name": q["name"], "threshold": threshold, "status": "missing"} for q in body["questions"]],
        fallback=reason,
    )


def parse_answers(body, response, threshold) -> JsonObject:
    """An identifiable bad answer cannot discard an independent good answer."""
    questions = {q["name"]: q for q in body["questions"]}
    if not isinstance(response, dict) or not isinstance(response.get("answers"), list):
        return failed_answers(body, threshold, "malformed")
    supplied = response["answers"]
    if len(supplied) > 2 * len(questions) or any(
        not isinstance(a, dict) or not isinstance(a.get("name"), str) or a["name"] not in questions for a in supplied
    ):
        return failed_answers(body, threshold, "malformed")
    by_name = defaultdict(list)
    for answer in supplied:
        by_name[answer["name"]].append(answer)
    rows = []
    for name, question in questions.items():
        matches = by_name[name]
        if len(matches) != 1:
            rows.append({"name": name, "threshold": threshold, "status": "malformed" if matches else "missing"})
        else:
            rows.append(outcome(question, matches[0], threshold))
    return assemble(rows)


def validate_outcomes(body, result, threshold) -> JsonObject:
    """Recheck broker diagnostics without fabricating refusals for omissions."""
    rows = result.get("questionResults")
    questions = {q["name"]: q for q in body["questions"]}
    if (
        not isinstance(rows, list)
        or len(rows) != len(questions)
        or any(not isinstance(r, dict) or not isinstance(r.get("name"), str) for r in rows)
        or {r["name"] for r in rows} != set(questions)
    ):
        return failed_answers(body, threshold, "malformed_broker_output")
    by_name = {r["name"]: r for r in rows}
    checked = []
    for name, question in questions.items():
        row = by_name[name]
        if (
            not isinstance(row.get("status"), str)
            or row["status"] not in STATUSES
            or not probability(row.get("threshold"))
            or row["threshold"] != threshold
        ):
            return failed_answers(body, threshold, "malformed_broker_output")
        if row["status"] in {"missing", "malformed"}:
            if "rawAnswer" in row:
                return failed_answers(body, threshold, "malformed_broker_output")
            checked.append({"name": name, "status": row["status"], "threshold": threshold})
        else:
            parsed = outcome(question, row.get("rawAnswer"), threshold)
            if parsed["status"] != row["status"]:
                return failed_answers(body, threshold, "malformed_broker_output")
            checked.append(parsed)
    reason = result.get("fallback")
    unavailable = isinstance(reason, str) and reason in UNAVAILABLE and all(r["status"] == "missing" for r in checked)
    parsed = assemble(checked, fallback=reason if unavailable else None)
    if result.get("answers") != parsed["answers"] or result.get("rawAnswers") != parsed["rawAnswers"]:
        return failed_answers(body, threshold, "malformed_broker_output")
    if result.get("fallback") != parsed["fallback"]:
        return failed_answers(body, threshold, "malformed_broker_output")
    return parsed
