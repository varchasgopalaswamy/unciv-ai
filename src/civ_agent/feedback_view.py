"""Share repeated feedback facts without changing their contents or meaning."""

import copy
import hashlib
from collections import Counter

from civ_agent.payload_updates import encoded
from civ_agent.protocol import JsonObject

FIELDS = {"observedChanges", "recentSubjectProgress", "currentCities", "currentUnits", "investment"}
INSTRUCTIONS = (
    "decisionFeedback.sharedFacts stores repeated factual objects once. In a choice, fieldRefs maps an omitted "
    "field name to its sharedFacts ID; use that exact object as the field's value. These references describe "
    "your earlier expectations and observations, not evidence that an investment caused an outcome."
)


def compact_feedback(value: JsonObject) -> JsonObject:
    result = copy.deepcopy(value)
    counts = Counter(encoded(choice[key]) for choice in result.get("choices", []) for key in FIELDS if key in choice)
    shared = {}
    for choice in result.get("choices", []):
        refs = {}
        for key in sorted(FIELDS & choice.keys()):
            text = encoded(choice[key])
            if counts[text] < 2 or len(text) < 200:
                continue
            id = "f" + hashlib.sha256(text.encode()).hexdigest()[:12]
            shared[id] = choice.pop(key)
            refs[key] = id
        if refs:
            choice["fieldRefs"] = refs
    if shared:
        result["sharedFacts"] = shared
    return result


def expand_feedback(value: JsonObject) -> JsonObject:
    result = copy.deepcopy(value)
    shared = result.pop("sharedFacts", {})
    for choice in result.get("choices", []):
        for field, id in choice.pop("fieldRefs", {}).items():
            choice[field] = copy.deepcopy(shared[id])
    return result
