"""Conversation-scoped notebook read delivery, separate from notebook mutations."""

import copy
import hashlib
from typing import Literal

import jsonpatch
from pydantic import Field, model_validator

from civ_agent.payload_updates import encoded, exact_patch
from civ_agent.protocol import JsonObject
from civ_agent.schemas import BoundaryModel, NonnegativeInt

INSTRUCTIONS = (
    "memoryDelivery describes every record returned by this read, in items order. "
    "A full item supplies its complete record in records; unchanged keeps that ID's last delivered read value; "
    "delta applies JSON Patch to that read value. Keep each item's stateId, version and the delivery's scopeId "
    "and sequence. Match baseStateId before applying changes, and ignore older versions or sequences. "
    "These are notebook delivery versions, not permission to use stale game observations. "
    "Missing search/read matches do not delete records. history=true returns full historical evidence separately. "
    "A notebook write or an automatic memoryUpdates record invalidates that ID's read baseline. "
    "If a base is missing, read its ID with memory_read(delivery=full, includeRetired=true)."
)


class MemoryDeliveryItem(BoundaryModel):
    id: str
    version: NonnegativeInt
    kind: Literal["full", "unchanged", "delta"]
    stateId: str
    baseStateId: str | None = None
    patch: list[JsonObject] | None = None

    @model_validator(mode="after")
    def valid_shape(self):
        if self.kind == "full" and (self.baseStateId is not None or self.patch is not None):
            raise ValueError("Full records have no baseline or patch")
        if self.kind != "full" and self.baseStateId is None:
            raise ValueError("Compact records require a baseline")
        if (self.kind == "delta") != (self.patch is not None):
            raise ValueError("Only delta records have a patch")
        return self


class MemoryDelivery(BoundaryModel):
    scopeId: str
    sequence: NonnegativeInt
    items: list[MemoryDeliveryItem] = Field(default_factory=list)

    @model_validator(mode="after")
    def unique_ids(self):
        if len({item.id for item in self.items}) != len(self.items):
            raise ValueError("A read delivers each record ID once")
        return self


def record_state_id(scope: str, id: str, version: int, value: JsonObject) -> str:
    return "m" + hashlib.sha256(encoded([scope, id, version, value]).encode()).hexdigest()[:24]


def memory_response(
    result: JsonObject,
    versions: dict[str, int],
    baselines: dict[str, JsonObject],
    scope: str,
    sequence: int,
    *,
    full: bool = False,
    historical: bool = False,
) -> JsonObject:
    """Select smaller complete wire alternatives after the authorized read."""
    if historical or any(record.get("historical") for record in result.get("records", [])):
        return copy.deepcopy(result)
    response = copy.deepcopy(result)
    records, items = [], []
    for record in result["records"]:
        id = record["id"]
        version = versions[id]
        item: JsonObject = {
            "id": id,
            "version": version,
            "kind": "full",
            "stateId": record_state_id(scope, id, version, record),
        }
        previous = baselines.get(id)
        if (
            not full
            and previous is not None
            and previous["scopeId"] == scope
            and previous["version"] <= version
            and previous["sequence"] < sequence
        ):
            candidate = {**item, "baseStateId": previous["stateId"]}
            if encoded(previous["value"]) == encoded(record):
                candidate["kind"] = "unchanged"
            else:
                patch = exact_patch(previous["value"], record)
                if patch is not None:
                    candidate.update(kind="delta", patch=patch)
            if candidate["kind"] != "full" and len(encoded({"records": [], "items": [candidate]})) < len(
                encoded({"records": [record], "items": [item]})
            ):
                item = candidate
        if item["kind"] == "full":
            records.append(copy.deepcopy(record))
        items.append(item)
    delivery = MemoryDelivery(scopeId=scope, sequence=sequence, items=items).model_dump(exclude_none=True)
    response.update(records=records, memoryDelivery=delivery)
    return response


def apply_memory_delivery(
    baselines: dict[str, JsonObject],
    result: JsonObject,
    *,
    scope: str | None = None,
    minimum_versions: dict[str, int] | None = None,
) -> tuple[JsonObject, dict[str, JsonObject]]:
    """Atomically reconstruct accepted rows without reviving older notebook state.

    A stale replay retains the newer cached value. A stale row whose read cache
    was invalidated by a newer write is ignored. Callers update their public
    record cache from the returned baselines, never from stale response rows.
    """
    expanded = copy.deepcopy(result)
    cached = copy.deepcopy(baselines)
    raw_delivery = expanded.pop("memoryDelivery", None)
    if raw_delivery is None:
        return expanded, cached
    delivery = MemoryDelivery.model_validate(raw_delivery)
    if scope is not None and delivery.scopeId != scope:
        raise ValueError("FULL_RECORD_REQUIRED: scope mismatch")
    records = {record["id"]: record for record in expanded["records"]}
    full_ids = {item.id for item in delivery.items if item.kind == "full"}
    if len(records) != len(expanded["records"]) or set(records) != full_ids:
        raise ValueError("FULL_RECORD_REQUIRED: full record identities mismatch")
    if any(record.get("historical") for record in records.values()):
        raise ValueError("FULL_RECORD_REQUIRED: historical records have no current baseline")
    rows = []
    for item in delivery.items:
        previous = cached.get(item.id)
        if item.version < (minimum_versions or {}).get(item.id, 0):
            if previous is not None and previous["version"] < (minimum_versions or {})[item.id]:
                cached.pop(item.id)
            continue
        if previous is not None and previous["scopeId"] == delivery.scopeId:
            if item.version < previous["version"] or delivery.sequence <= previous["sequence"]:
                rows.append(copy.deepcopy(previous["value"]))
                continue
        if item.kind == "full":
            record = records[item.id]
        else:
            if (
                previous is None
                or previous["scopeId"] != delivery.scopeId
                or previous["stateId"] != item.baseStateId
                or previous["version"] > item.version
            ):
                raise ValueError("FULL_RECORD_REQUIRED")
            record = copy.deepcopy(previous["value"])
            if item.kind == "delta":
                try:
                    record = jsonpatch.apply_patch(record, item.patch)
                except (jsonpatch.JsonPatchException, KeyError, IndexError, TypeError) as error:
                    raise ValueError("FULL_RECORD_REQUIRED: invalid record patch") from error
        if (
            not isinstance(record, dict)
            or record.get("id") != item.id
            or record.get("historical")
            or item.stateId != record_state_id(delivery.scopeId, item.id, item.version, record)
        ):
            raise ValueError("FULL_RECORD_REQUIRED: record state mismatch")
        cached[item.id] = {
            "scopeId": delivery.scopeId,
            "sequence": delivery.sequence,
            "id": item.id,
            "version": item.version,
            "stateId": item.stateId,
            "value": copy.deepcopy(record),
        }
        rows.append(copy.deepcopy(record))
    expanded["records"] = rows
    return expanded, cached
