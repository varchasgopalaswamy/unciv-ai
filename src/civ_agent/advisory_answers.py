"""Typed per-question advisory outcomes, independent of delivery or accounting."""

import math
from collections import defaultdict
from collections.abc import Collection
from typing import Annotated, Literal, NotRequired, TypedDict

from pydantic import BeforeValidator, ConfigDict, Field, TypeAdapter, ValidationError, with_config

from civ_agent.protocol import JsonObject

STATUSES = {"accepted", "low_confidence", "refusal", "malformed", "missing"}
UNAVAILABLE = {
    "budget_exhausted",
    "completion_unknown",
    "timeout",
    "service_failure",
    "circuit_open",
    "malformed",
    "source_epoch_closed",
    "task_allowance_exhausted",
    "task_attribution_unavailable",
}


def _json_number(value):
    # Strict floats also accept Decimal and objects with __float__. This owned
    # JSON boundary accepts the original int/float primitives only.
    if type(value) not in (int, float):
        raise ValueError("Invalid probability")
    return value


type Probability = Annotated[int | float, BeforeValidator(_json_number), Field(ge=0, le=1, allow_inf_nan=False)]

_ANSWER_CONFIG = ConfigDict(strict=True, extra="ignore", hide_input_in_errors=True)


@with_config(_ANSWER_CONFIG)
class ProbabilityRow(TypedDict):
    value: str
    probability: Probability


@with_config(_ANSWER_CONFIG)
class ChoiceAnswer(TypedDict):
    name: str
    type: Literal["choice"]
    choice: str
    confidence: Probability
    probability: NotRequired[Probability]
    providerConfidence: NotRequired[Probability]
    probabilities: NotRequired[list[ProbabilityRow]]


@with_config(_ANSWER_CONFIG)
class RefusalAnswer(TypedDict):
    name: str
    type: Literal["refusal"]


_PROBABILITY = TypeAdapter(Probability, config=ConfigDict(strict=True))
_VECTOR = TypeAdapter(list[ProbabilityRow])
_ANSWER = TypeAdapter(Annotated[ChoiceAnswer | RefusalAnswer, Field(discriminator="type")])


def probability(value):
    try:
        _PROBABILITY.validate_python(value)
    except ValidationError:
        return False
    return True


def _complete_vector(
    choices: Collection[str], values: list[ProbabilityRow], tolerance: float, *, relative_tolerance: float = 0
) -> bool:
    return (
        len(values) == len(choices)
        and len({row["value"] for row in values}) == len(choices)
        and {row["value"] for row in values} == set(choices)
        and math.isclose(sum(row["probability"] for row in values), 1, rel_tol=relative_tolerance, abs_tol=tolerance)
    )


def choice_probabilities(choices: Collection[str], value: object, *, tolerance: float) -> list[JsonObject] | None:
    """Validate an independent distribution, retaining the supplied order."""
    try:
        values = _VECTOR.validate_python(value, strict=True)
    except ValidationError:
        return None
    return [dict(row) for row in values] if _complete_vector(choices, values, tolerance) else None


def typed_answer(question, answer):
    """Retain only the API's typed fields, never refusal/provider prose."""
    try:
        parsed = _ANSWER.validate_python(answer)
    except ValidationError:
        return None
    if parsed["name"] != question["name"]:
        return None
    raw: JsonObject = dict(parsed)
    if parsed["type"] == "refusal":
        return raw
    choices = [c["value"] for c in question["choices"]]
    if parsed["choice"] not in choices:
        return None
    if "probabilities" in parsed:
        values = parsed["probabilities"]
        if not _complete_vector(choices, values, 0.01, relative_tolerance=1e-9):
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
