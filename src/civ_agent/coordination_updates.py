"""Lossless conversation-scoped delivery of civilization-local coordination."""

import copy
import hashlib
import json

from civ_agent.protocol import JsonObject
from civ_agent.state_updates import apply_overview_update, overview_update, same_json

META = {"stateId", "scopeId", "sequence"}
MESSAGE_PATH = "warState.messages"
COLLECTIONS = {"requests": "id", MESSAGE_PATH: "id"}
INSTRUCTIONS = (
    "economyCoordination is a complete snapshot of your bounded working context; history remains retrievable. "
    "Keep its scopeId, sequence and stateId. coordinationUpdate contains subsequent changes: "
    "set replaces named fields, removed deletes fields, and collections upserts/removes rows by their key; "
    "order, when present, gives the complete resulting row order. warState.messages is a separate keyed "
    "collection inside warState: replacing other warState fields preserves that message collection. "
    "Omitted coordination stays unchanged. Ignore an already applied sequence; never apply an older response "
    "over newer state. If the scope or baseStateId does not match, read player_turn for a full snapshot. "
    "Current goals, request dispositions, overrides and role ownership remain authoritative. "
    "war_goals provides filtered, paginated history reads."
)


def context_compacted(method: str, params: JsonObject) -> bool:
    return method == "thread/compacted" or (
        method == "item/completed" and (params.get("item") or {}).get("type") == "contextCompaction"
    )


def content(value: JsonObject) -> JsonObject:
    return {key: copy.deepcopy(item) for key, item in value.items() if key not in META}


def snapshot(value: JsonObject, job: str, epoch: str, scope: str, sequence: int) -> JsonObject:
    current = content(value)
    encoded = json.dumps([job, epoch, scope, current], sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return {
        **current,
        "stateId": "c" + hashlib.sha256(encoded.encode()).hexdigest()[:24],
        "scopeId": scope,
        "sequence": sequence,
    }


def _flat(value: JsonObject) -> JsonObject:
    result = copy.deepcopy(value)
    war = result.get("warState")
    if isinstance(war, dict) and "messages" in war:
        result[MESSAGE_PATH] = war.pop("messages")
    return result


def _nested(value: JsonObject) -> JsonObject:
    result = copy.deepcopy(value)
    if MESSAGE_PATH in result:
        result["warState"]["messages"] = result.pop(MESSAGE_PATH)
    return result


def response(previous: JsonObject | None, current: JsonObject, *, full: bool = False) -> JsonObject:
    if full or previous is None or previous["scopeId"] != current["scopeId"]:
        return {"economyCoordination": copy.deepcopy(current)}
    if same_json(content(previous), content(current)):
        return {}
    delta = overview_update(_flat(previous), _flat(current), [], collection_keys=COLLECTIONS)
    delta.pop("invalidatedDetails", None)
    # These are delivery metadata, not coordination field replacements.
    delta["scopeId"] = current["scopeId"]
    delta["sequence"] = current["sequence"]
    for key in ("scopeId", "sequence"):
        delta["set"].pop(key, None)
    for key in ("set", "removed", "collections"):
        if not delta[key]:
            delta.pop(key)
    return {"coordinationUpdate": delta}


def apply_coordination_update(previous: JsonObject | None, result: JsonObject) -> JsonObject | None:
    """Reference consumer; duplicate/older responses cannot rewind delivered state."""
    full = result.get("economyCoordination")
    update = result.get("coordinationUpdate")
    value = full if full is not None else update
    if value is None:
        return copy.deepcopy(previous)
    if previous is not None and value.get("scopeId") == previous.get("scopeId"):
        if value["sequence"] <= previous["sequence"]:
            return copy.deepcopy(previous)
    if full is not None:
        return copy.deepcopy(full)
    assert update is not None
    if previous is None or update["scopeId"] != previous["scopeId"]:
        raise ValueError("FULL_COORDINATION_REQUIRED: read player_turn")
    flat = apply_overview_update(_flat(previous), update)
    flat.update(scopeId=update["scopeId"], sequence=update["sequence"])
    return _nested(flat)


def payload_characters(result: JsonObject) -> dict[str, int]:
    def size(value):
        return len(json.dumps(value, ensure_ascii=False, separators=(",", ":")))

    full, delta = result.get("economyCoordination"), result.get("coordinationUpdate")
    messages = ((full or {}).get("warState") or {}).get("messages", [])
    changed = ((delta or {}).get("collections") or {}).get(MESSAGE_PATH, {}).get("upsert", [])
    detail = result.get("detailUpdate") or {}
    changes = result.get("changes") or []
    return {
        "responses": 1,
        "characters": size(result),
        "coordinationFull": size(full) if full is not None else 0,
        "coordinationDelta": size(delta) if delta is not None else 0,
        "coordinationMessages": size(messages) if messages else 0,
        "coordinationChangedMessages": size(changed) if changed else 0,
        "detailFull": size(result["data"]) if detail.get("kind") == "full" and "data" in result else 0,
        "detailDelta": size(detail) if detail.get("kind") == "delta" else 0,
        "detailUnchangedResponses": int(detail.get("kind") == "unchanged"),
        "memoryRecordUpdates": sum(size(change["recordUpdate"]) for change in changes if "recordUpdate" in change),
    }
