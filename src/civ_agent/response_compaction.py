"""Expand message-local sharing before any observation or detail reconstruction."""

import copy

from civ_agent.construction_explanations import compact_explanations, expand_explanations
from civ_agent.protocol import JsonObject
from civ_agent.shared_values import compact_shared_values, expand_shared_values


def expand_response(value: JsonObject, *, startup=False) -> JsonObject:
    value = copy.deepcopy(value)
    if isinstance(value.get("stateUpdate"), dict):
        value["stateUpdate"] = expand_shared_values(value["stateUpdate"])
    return expand_explanations(expand_shared_values(value, startup=startup), startup=startup)


def compact_response(value: JsonObject, *, startup=False, full=False, share=True) -> JsonObject:
    if full:
        return expand_response(value, startup=startup)
    value = compact_explanations(value, startup=startup)
    if not share:
        return value
    if isinstance(value.get("stateUpdate"), dict):
        value["stateUpdate"] = compact_shared_values(value["stateUpdate"])
    return compact_shared_values(value, startup=startup)
