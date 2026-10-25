"""Deterministic updates of delivered overviews, never hidden engine state."""

import copy
import hashlib
import json
from typing import Literal

import jsonpatch
from pydantic import Field

from civ_agent.observations import EXTERNAL_DETAIL_TOOLS, DetailQuery, project_detail
from civ_agent.payload_updates import encoded, exact_patch
from civ_agent.protocol import JsonObject
from civ_agent.schemas import BoundaryModel

# Rows have stable player-visible identities. Other sections are replaced as a
# whole when changed, keeping the format readable and avoiding array-index edits.
COLLECTION_KEYS = {
    "units": "handle",
    "cities": "handle",
    "alerts": "handle",
    "notifications": "handle",
    "diplomacy": "civilizationId",
    "cityStates": "civilizationId",
    "wonderOpportunities": "name",
}


class CollectionUpdate(BoundaryModel):
    key: str
    upsert: list[JsonObject] = Field(default_factory=list)
    removed: list[str] = Field(default_factory=list)
    order: list[str] | None = None


class OverviewDelta(BoundaryModel):
    kind: Literal["delta"] = "delta"
    baseStateId: str
    stateId: str
    set: JsonObject = Field(default_factory=dict)
    removed: list[str] = Field(default_factory=list)
    collections: dict[str, CollectionUpdate] = Field(default_factory=dict)
    invalidatedDetails: list[DetailQuery] = Field(default_factory=list)
    patch: list[JsonObject] = Field(default_factory=list, exclude_if=lambda value: not value)


def stamp(overview: JsonObject, job: str, epoch: str, revision: str) -> JsonObject:
    content = json.dumps([job, epoch, revision, overview], sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return {**overview, "stateId": "s" + hashlib.sha256(content.encode()).hexdigest()[:24]}


def same_json(before, after) -> bool:
    # Python equates True with 1; an exact JSON update must preserve their types.
    return json.dumps(before, sort_keys=True, ensure_ascii=False, separators=(",", ":")) == json.dumps(
        after, sort_keys=True, ensure_ascii=False, separators=(",", ":")
    )


def indexed(rows, key):
    if not isinstance(rows, list) or any(
        not isinstance(row, dict) or not isinstance(row.get(key), str) for row in rows
    ):
        return None
    result = {row[key]: row for row in rows}
    return result if len(result) == len(rows) else None


def overview_update(
    previous: JsonObject | None,
    current: JsonObject,
    invalidated: list[DetailQuery],
    *,
    collection_keys: dict[str, str] | None = None,
    compact_nested: bool = False,
    allow_unchanged: bool = False,
) -> JsonObject:
    if previous is None:
        return {
            "kind": "full",
            "overview": copy.deepcopy(current),
            "invalidatedDetails": [q.model_dump() for q in invalidated],
        }
    if allow_unchanged and same_json(
        {k: v for k, v in previous.items() if k != "stateId"},
        {k: v for k, v in current.items() if k != "stateId"},
    ):
        return {
            "kind": "unchanged",
            "baseStateId": previous["stateId"],
            "stateId": current["stateId"],
            "invalidatedDetails": [q.model_dump() for q in invalidated],
        }
    update = OverviewDelta(baseStateId=previous["stateId"], stateId=current["stateId"], invalidatedDetails=invalidated)
    update.removed = sorted(previous.keys() - current.keys())
    for field in sorted(current.keys() - {"stateId"}):
        if field in previous and same_json(current[field], previous[field]):
            continue
        if (
            not compact_nested
            and field in {"decisionFeedback", "recentProgress"}
            and isinstance(previous.get(field), dict)
            and isinstance(current[field], dict)
        ):
            patch = jsonpatch.make_patch({field: previous[field]}, {field: current[field]}).patch
            if len(json.dumps(patch)) < len(json.dumps(current[field])) and same_json(
                jsonpatch.apply_patch({field: previous[field]}, patch), {field: current[field]}
            ):
                update.patch.extend(patch)
                continue
        key = (COLLECTION_KEYS if collection_keys is None else collection_keys).get(field)
        before = indexed(previous.get(field), key) if key else None
        after = indexed(current[field], key) if key else None
        if key is None or before is None or after is None:
            update.set[field] = copy.deepcopy(current[field])
            continue
        patch = CollectionUpdate(
            key=key,
            upsert=[copy.deepcopy(row) for id, row in after.items() if not same_json(before.get(id), row)],
            removed=sorted(before.keys() - after.keys()),
        )
        if list(before) != list(after):
            patch.order = list(after)
        update.collections[field] = patch
    result = update.model_dump(exclude_none=True)
    if compact_nested:
        for field in sorted(set(result["set"]) | set(result["collections"])):
            if field not in previous or not isinstance(previous[field], (dict, list)):
                continue
            if not isinstance(current[field], (dict, list)):
                continue
            patch = exact_patch({field: previous[field]}, {field: current[field]})
            if patch is None:
                continue
            prefix = "/" + field.replace("~", "~0").replace("/", "~1")
            if any(
                not (operation[path] == prefix or operation[path].startswith(prefix + "/"))
                for operation in patch
                for path in ("path", "from")
                if path in operation
            ):
                continue
            candidate = copy.deepcopy(result)
            candidate["set"].pop(field, None)
            candidate["collections"].pop(field, None)
            candidate.setdefault("patch", []).extend(patch)
            if len(encoded(candidate)) < len(encoded(result)):
                result = candidate
    return result


def apply_overview_update(previous: JsonObject | None, update: JsonObject) -> JsonObject:
    """Reference reducer for consumers/tests. A replay never reapplies an old delta."""
    from civ_agent.response_compaction import expand_response

    previous = expand_response(previous) if previous is not None else None
    update = expand_response(update)
    if previous is not None:
        previous = {
            key: value
            for key, value in previous.items()
            if key not in {"memoryUpdates", "invalidatedDetails", "economyCoordination", "coordinationUpdate"}
        }
    if update["kind"] == "full":
        return copy.deepcopy(update["overview"])
    if update["kind"] not in {"delta", "unchanged"} or previous is None:
        raise ValueError("FULL_OVERVIEW_REQUIRED")
    if previous.get("stateId") == update["stateId"]:
        return copy.deepcopy(previous)
    if previous.get("stateId") != update["baseStateId"]:
        raise ValueError("BASE_STATE_MISMATCH: read player_turn(delivery=full)")
    current = copy.deepcopy(previous)
    if update["kind"] == "unchanged":
        current["stateId"] = update["stateId"]
        return current
    for field in update.get("removed", []):
        current.pop(field, None)
    current.update(copy.deepcopy(update.get("set", {})))
    for field, patch in update.get("collections", {}).items():
        rows = {row[patch["key"]]: row for row in current[field]}
        for id in patch["removed"]:
            rows.pop(id, None)
        rows.update({row[patch["key"]]: copy.deepcopy(row) for row in patch["upsert"]})
        current[field] = [rows[id] for id in patch.get("order", list(rows))]
    if update.get("patch"):
        current = jsonpatch.apply_patch(current, update["patch"])
    current["stateId"] = update["stateId"]
    return current


def apply_player_turn(previous: JsonObject | None, result: JsonObject) -> JsonObject:
    """Consume either a complete overview or a compact explicit refresh."""
    from civ_agent.response_compaction import expand_response

    result = expand_response(result)
    if "stateUpdate" in result:
        return apply_overview_update(previous, result["stateUpdate"])
    if "stateId" not in result:
        raise ValueError("FULL_OVERVIEW_REQUIRED")
    return {
        key: value
        for key, value in result.items()
        if key not in {"memoryUpdates", "invalidatedDetails", "economyCoordination", "coordinationUpdate"}
    }


def changed_details(previous: JsonObject | None, current: JsonObject, reads: list[DetailQuery]) -> list[DetailQuery]:
    changed = []
    for query in reads:
        if query.tool in EXTERNAL_DETAIL_TOOLS:
            if (
                previous is None
                or previous.get("revision") != current.get("revision")
                or not same_json(previous.get("communication"), current.get("communication"))
            ):
                changed.append(query)
            continue
        if (
            previous is None
            or (
                query.tool in {"movement_preview", "mission_preview", "player_inspect"}
                and previous.get("revision") != current.get("revision")
            )
            or (
                query.tool not in {"movement_preview", "mission_preview", "player_inspect"}
                and not same_json(project_detail(previous, query), project_detail(current, query))
            )
        ):
            changed.append(query)
    return changed


def refresh_required() -> JsonObject:
    return {
        "kind": "unavailable",
        "reason": "REFRESH_FAILED",
        "recovery": {
            "reads": [{"tool": "player_turn", "arguments": {}}],
            "instruction": "The action succeeded, but its state refresh failed. Keep the receipt; do not repeat the action. Read player_turn before continuing.",
        },
    }
