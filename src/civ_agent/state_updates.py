"""Deterministic updates of delivered overviews, never hidden engine state."""

import copy
import hashlib
import json
from typing import Literal

from pydantic import Field

from civ_agent.observations import EXTERNAL_DETAIL_TOOLS, DetailQuery, project_detail
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


def overview_update(previous: JsonObject | None, current: JsonObject, invalidated: list[DetailQuery]) -> JsonObject:
    if previous is None:
        return {
            "kind": "full",
            "overview": copy.deepcopy(current),
            "invalidatedDetails": [q.model_dump() for q in invalidated],
        }
    update = OverviewDelta(baseStateId=previous["stateId"], stateId=current["stateId"], invalidatedDetails=invalidated)
    update.removed = sorted(previous.keys() - current.keys())
    for field in sorted(current.keys() - {"stateId"}):
        if field in previous and same_json(current[field], previous[field]):
            continue
        key = COLLECTION_KEYS.get(field)
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
    return update.model_dump(exclude_none=True)


def apply_overview_update(previous: JsonObject | None, update: JsonObject) -> JsonObject:
    """Reference reducer for consumers/tests. A replay never reapplies an old delta."""
    if previous is not None:
        previous = {key: value for key, value in previous.items() if key not in {"memoryUpdates", "invalidatedDetails"}}
    if update["kind"] == "full":
        return copy.deepcopy(update["overview"])
    if update["kind"] != "delta" or previous is None:
        raise ValueError("FULL_OVERVIEW_REQUIRED")
    if previous.get("stateId") == update["stateId"]:
        return copy.deepcopy(previous)
    if previous.get("stateId") != update["baseStateId"]:
        raise ValueError("BASE_STATE_MISMATCH: read player_turn for a full overview")
    current = copy.deepcopy(previous)
    for field in update.get("removed", []):
        current.pop(field, None)
    current.update(copy.deepcopy(update.get("set", {})))
    for field, patch in update.get("collections", {}).items():
        rows = {row[patch["key"]]: row for row in current[field]}
        for id in patch["removed"]:
            rows.pop(id, None)
        rows.update({row[patch["key"]]: copy.deepcopy(row) for row in patch["upsert"]})
        current[field] = [rows[id] for id in patch.get("order", list(rows))]
    current["stateId"] = update["stateId"]
    return current


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
                query.tool in {"movement_preview", "mission_preview"}
                and previous.get("revision") != current.get("revision")
            )
            or (
                query.tool not in {"movement_preview", "mission_preview"}
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
