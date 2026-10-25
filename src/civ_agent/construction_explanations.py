"""Self-contained sharing of exact construction explanations at owned locations."""

import copy
from collections import Counter
from collections.abc import Iterator

from civ_agent.construction_estimates import ESTIMATE_BASIS, INVESTMENT_BASIS
from civ_agent.payload_updates import encoded
from civ_agent.protocol import JsonObject

EXPLANATIONS = {"estimate": ESTIMATE_BASIS, "investment": INVESTMENT_BASIS}
INSTRUCTIONS = (
    "constructionExplanations stores complete repeated construction guidance once in this message. "
    "At an affected object, explanationRefs maps an omitted field to its exact explanation ID. "
    "Expand those fields before applying state/detail updates. These references are local to this message, "
    "not earlier replies. All estimate caveats still apply to every referencing construction. "
    "A delivery=full detail recovery read includes the original explanation text."
)


def _detail_fields(reply: JsonObject) -> Iterator[tuple[JsonObject, str, str]]:
    query, data = reply.get("query", {}), reply.get("data", {})
    if not isinstance(query, dict) or not isinstance(data, dict):
        return
    if query.get("tool") == "city_details":
        entity = data.get("entity")
        if isinstance(entity, dict):
            yield entity, "constructionEstimateBasis", "estimate"
    if query.get("tool") == "construction_details":
        yield data, "estimateBasis", "estimate"
        rows = data.get("constructions", [])
        if isinstance(rows, list):
            for row in rows:
                comparison = row.get("investmentComparison") if isinstance(row, dict) else None
                if isinstance(comparison, dict):
                    yield comparison, "basis", "investment"


def _fields(value: JsonObject, *, startup: bool) -> Iterator[tuple[JsonObject, str, str]]:
    if startup:
        overview = value.get("reads", {}).get("player_turn")
        if isinstance(overview, dict):
            yield overview, "constructionEstimateBasis", "estimate"
        for reply in value.get("decisionDetails", []):
            if isinstance(reply, dict):
                yield from _detail_fields(reply)
        return
    if (
        "cities" in value
        or "constructionEstimateBasis" in value
        or "constructionEstimateBasis" in (value.get("explanationRefs") or {})
    ):
        yield value, "constructionEstimateBasis", "estimate"
    yield from _detail_fields(value)
    update = value.get("stateUpdate")
    if isinstance(update, dict):
        # The overview-level basis is the sole explanatory field in ordinary
        # overview data. Patch values are deliberately outside this projection.
        for key in ("overview", "set"):
            overview = update.get(key)
            if isinstance(overview, dict):
                yield overview, "constructionEstimateBasis", "estimate"


def compact_explanations(value: JsonObject, *, startup: bool = False, full: bool = False) -> JsonObject:
    if full:
        return expand_explanations(value, startup=startup)
    if "constructionExplanations" in value:
        return copy.deepcopy(value)
    result = copy.deepcopy(value)
    counts = Counter(
        name
        for obj, field, name in _fields(result, startup=startup)
        if obj.get(field) == EXPLANATIONS[name] and "explanationRefs" not in obj
    )
    for name in sorted(counts):
        if counts[name] < 2:
            continue
        candidate = copy.deepcopy(result)
        for obj, field, reference in _fields(candidate, startup=startup):
            if reference == name and obj.get(field) == EXPLANATIONS[name] and "explanationRefs" not in obj:
                obj.pop(field)
                obj["explanationRefs"] = {field: name}
        candidate.setdefault("constructionExplanations", {})[name] = EXPLANATIONS[name]
        if len(encoded(candidate)) < len(encoded(result)):
            result = candidate
    if encoded(expand_explanations(result, startup=startup)) != encoded(value):
        raise ValueError("CONSTRUCTION_EXPLANATION_MISMATCH")
    return result


def expand_explanations(value: JsonObject, *, startup: bool = False) -> JsonObject:
    result = copy.deepcopy(value)
    registry = result.pop("constructionExplanations", None)
    if registry is None:
        if any("explanationRefs" in obj for obj, _, _ in _fields(result, startup=startup)):
            raise ValueError("FULL_EXPLANATIONS_REQUIRED")
        return result
    if (
        not isinstance(registry, dict)
        or not registry
        or set(registry) - EXPLANATIONS.keys()
        or any(not isinstance(text, str) for text in registry.values())
    ):
        raise ValueError("FULL_EXPLANATIONS_REQUIRED")
    used = set()
    for obj, field, name in _fields(result, startup=startup):
        refs = obj.get("explanationRefs")
        if refs is None:
            continue
        if refs != {field: name} or name not in registry or field in obj:
            raise ValueError("FULL_EXPLANATIONS_REQUIRED")
        obj[field] = registry[name]
        obj.pop("explanationRefs")
        used.add(name)
    if used != set(registry):
        raise ValueError("FULL_EXPLANATIONS_REQUIRED")
    return result
