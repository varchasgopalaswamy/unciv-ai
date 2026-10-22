"""Provider wire formats for the common, probability-based Decisions contract."""

from dataclasses import dataclass
from typing import Literal

from civ_agent.protocol import JsonObject

DecisionBackend = Literal["luna", "jev"]
DEFAULT_BACKEND: DecisionBackend = "jev"


@dataclass(frozen=True)
class Backend:
    model: str
    endpoint: str
    acceptance_probability: float
    nanodollars_per_input_token: int
    rate_version: str


BACKENDS = {
    "luna": Backend(
        "gpt-6-luna",
        "https://api.openai.com/v1/decisions",
        0.95,
        100,
        "decisions-standard-2026-10-14-v1",
    ),
    "jev": Backend(
        "Winnow-12B",
        "http://127.0.0.1:8091/v1/systemone",
        0.98,
        0,
        "decisions-local-free-v1",
    ),
}


def provider_request(body, backend) -> JsonObject:
    if backend == "luna":
        return body
    return {
        "model": body["model"],
        "state": body["input"],
        "questions": {
            q["name"]: {
                "type": "choice",
                "instructions": q["instructions"],
                "criteria": {c["value"]: c["description"] for c in q["choices"]},
            }
            for q in body["questions"]
        },
    }


def provider_answers(body, response, backend) -> list[JsonObject]:
    """Normalize selected probability; keep native confidence as diagnostics.

    The legacy internal `confidence` field is the selected answer's probability
    in the common API. A provider's entropy/margin score never grants acceptance.
    Existing typed validators reject missing or invalid probability vectors.
    """
    from civ_agent.advisory_answers import typed_answer

    if not isinstance(response, dict) or response.get("model", body["model"]) != body["model"]:
        raise ValueError("Decisions response model mismatch")
    supplied = response.get("answers")
    if backend == "jev":
        if not isinstance(supplied, dict):
            raise ValueError("Invalid Jev answer map")
        supplied = [
            {
                **answer,
                "name": name,
                "probabilities": [
                    {"value": key, "probability": value} for key, value in answer.get("probabilities", {}).items()
                ]
                if isinstance(answer.get("probabilities"), dict)
                else None,
            }
            if isinstance(answer, dict)
            else {"name": name}
            for name, answer in supplied.items()
        ]
    if not isinstance(supplied, list):
        raise ValueError("Invalid Decisions answer list")
    questions = {q["name"]: q for q in body["questions"]}
    normalized = []
    for answer in supplied:
        if not isinstance(answer, dict) or answer.get("name") not in questions:
            raise ValueError("Unknown Decisions answer")
        raw: JsonObject | None = typed_answer(questions[answer["name"]], answer)
        if raw is None or raw.get("type") == "choice" and "probabilities" not in raw:
            normalized.append({"name": answer["name"]})
            continue
        if raw["type"] == "choice":
            selected = next(p["probability"] for p in raw["probabilities"] if p["value"] == raw["choice"])
            if selected < max(p["probability"] for p in raw["probabilities"]):
                normalized.append({"name": answer["name"]})
                continue
            raw["providerConfidence"] = raw["confidence"]
            raw["confidence"] = selected
            raw["probability"] = selected
        normalized.append(raw)
    return normalized
