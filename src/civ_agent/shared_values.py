"""Exact message-local sharing at service-owned native presentation fields."""

import copy
from collections import defaultdict
from collections.abc import Iterator

from civ_agent.payload_updates import encoded
from civ_agent.protocol import JsonObject

INSTRUCTIONS = (
    "sharedValues contains exact repeated native values once in this message. A field value containing only "
    "sharedValueRef is replaced by sharedValues[that ID]. Expand references before applying any state/detail "
    "update or interpreting its facts. References never use an earlier message's registry. delivery=full "
    "returns the inline values. Sharing never rewrites authored text or grants observation credit."
)

FIELDS = frozenset(
    {
        "basis",
        "warnings",
        "limitations",
        "requirements",
        "confirmations",
        "completion",
        "growth",
        "foodPerTurnByCity",
        "empireEffectIfCompletedNow",
        "whileBuilding",
        "yields",
        "citizens",
        "specialists",
        "visibleHostiles",
        "ownMilitary",
        "context",
        "unlinkedInvestment",
        "populationCosts",
        "buildings",
    }
)
# Complete result objects remain unchanged in code. These presentation shapes
# contain exact copies of the same payload under several documented paths.
RESULT_FIELDS = FIELDS | {"result", "receipt", "moves", "options", "overview", "approval", "report"}


def result_copies(value: JsonObject) -> bool:
    keys = value.keys() - {"sharedValues"}
    feedback = value.get("feedback")
    if isinstance(feedback, dict) and isinstance(feedback.get("receipts"), list):
        return (
            value.get("receiptVersion") == 1
            and isinstance(value.get("receiptEntries"), list)
            and isinstance(value.get("results"), list)
            or keys == {"feedback", "receipt"}
            or keys == {"feedback", "unit", "movement", "options"}
        )
    if keys == {"bundle", "query"} and isinstance(value["bundle"], dict):
        return result_copies(value["bundle"])
    consultation = value.get("consultation")
    return (
        value.get("error") == "MAIN_DECLINED_PEACE"
        and isinstance(consultation, dict)
        and isinstance(consultation.get("phases"), list)
    )


SKIP = frozenset(
    {
        "text",
        "reason",
        "reasons",
        "assessment",
        "intent",
        "messages",
        "choices",
        "strategyReview",
        "strategicAdvice",
        "memoryUpdates",
        "records",
        "memoryContext",
        "coordinationUpdate",
        "economyCoordination",
        "warState",
        "constructionExplanations",
        "sharedValues",
    }
)


def _roots(value: JsonObject, startup: bool) -> Iterator[tuple[tuple, JsonObject]]:
    if startup:
        for name in ("player_turn", "native_advice"):
            reply = value.get("reads", {}).get(name)
            if isinstance(reply, dict):
                yield ("reads", name), reply
        for index, reply in enumerate(value.get("decisionDetails", [])):
            for path, root in _roots(reply, False):
                yield ("decisionDetails", index, *path), root
        return
    if result_copies(value):
        yield (), value
        return
    if "stateId" in value and isinstance(value.get("cities"), list):
        yield (), value
        return
    if (
        value.get("kind") in {"full", "delta", "unchanged"}
        and "stateId" in value
        or (value.get("kind") == "full" and isinstance(value.get("overview"), dict))
    ):
        yield (), value
        return
    for field in ("data", "stateUpdate", "detailUpdate"):
        if isinstance(value.get(field), dict):
            yield (field,), value[field]
    for index, item in enumerate(value.get("results", [])):
        if isinstance(item, dict) and isinstance(item.get("result"), dict):
            for path, root in _roots(item["result"], False):
                yield ("results", index, "result", *path), root


def _fields(root, path=(), *, fields=FIELDS) -> Iterator[tuple[tuple, object]]:
    if isinstance(root, dict):
        for name, value in root.items():
            if name in SKIP:
                continue
            if name in fields:
                yield (*path, name), value
            yield from _fields(value, (*path, name), fields=fields)
    elif isinstance(root, list):
        for index, value in enumerate(root):
            yield from _fields(value, (*path, index), fields=fields)


def _set(value, path, replacement):
    for key in path[:-1]:
        value = value[key]
    value[path[-1]] = replacement


def _literal_reference(value):
    if isinstance(value, dict):
        return set(value) == {"sharedValueRef"} or any(_literal_reference(item) for item in value.values())
    if isinstance(value, list):
        return any(_literal_reference(item) for item in value)
    return False


def compact_shared_values(value: JsonObject, *, startup=False, full=False) -> JsonObject:
    if full:
        return expand_shared_values(value, startup=startup)
    if "sharedValues" in value:
        expand_shared_values(value, startup=startup)  # Validate, retaining idempotent wire bytes.
        return copy.deepcopy(value)
    if result_copies(value) and _literal_reference(value):
        return copy.deepcopy(value)
    candidates = defaultdict(list)
    originals = {}
    for prefix, root in _roots(value, startup):
        for path, item in _fields(root, prefix, fields=RESULT_FIELDS if result_copies(root) else FIELDS):
            if isinstance(item, dict) and set(item) == {"sharedValueRef"}:
                return copy.deepcopy(value)  # Never reinterpret an original native field.
            if not isinstance(item, (dict, list, str)):
                continue
            key = encoded(item)
            if len(key) >= 120:
                candidates[key].append(path)
                originals[key] = item
    result, selected = copy.deepcopy(value), []
    groups = sorted(candidates, key=lambda key: (-(len(candidates[key]) - 1) * len(key), key))
    for key in groups:
        paths = [
            path
            for path in candidates[key]
            if not any(path[: len(old)] == old or old[: len(path)] == path for old in selected)
        ]
        if len(paths) < 2:
            continue
        candidate = copy.deepcopy(result)
        registry = candidate.setdefault("sharedValues", {})
        reference = "v" + str(len(registry) + 1)
        registry[reference] = copy.deepcopy(originals[key])
        for path in paths:
            _set(candidate, path, {"sharedValueRef": reference})
        if len(encoded(candidate)) < len(encoded(result)):
            result = candidate
            selected.extend(paths)
            if len(registry) >= 16:
                break
    if encoded(expand_shared_values(result, startup=startup)) != encoded(value):
        raise ValueError("SHARED_VALUE_MISMATCH")
    return result


def expand_shared_values(value: JsonObject, *, startup=False) -> JsonObject:
    result = copy.deepcopy(value)
    registry = result.pop("sharedValues", None)
    if registry is None:
        return result
    if (
        not isinstance(registry, dict)
        or not 1 <= len(registry) <= 16
        or set(registry) != {"v" + str(index) for index in range(1, len(registry) + 1)}
        or any(not isinstance(item, (str, dict, list)) for item in registry.values())
    ):
        raise ValueError("FULL_SHARED_VALUES_REQUIRED")
    used = set()
    for prefix, root in _roots(result, startup):
        # Collect before replacing, so a registry value cannot add references.
        for path, item in list(_fields(root, prefix, fields=RESULT_FIELDS if result_copies(root) else FIELDS)):
            if not isinstance(item, dict) or set(item) != {"sharedValueRef"}:
                continue
            reference = item["sharedValueRef"]
            if not isinstance(reference, str) or reference not in registry:
                raise ValueError("FULL_SHARED_VALUES_REQUIRED")
            _set(result, path, copy.deepcopy(registry[reference]))
            used.add(reference)
    if used != set(registry):
        raise ValueError("FULL_SHARED_VALUES_REQUIRED")
    return result
