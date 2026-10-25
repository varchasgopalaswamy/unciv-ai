"""Lossless model-facing payloads, independent of native evidence registration."""

import copy
import hashlib
import json

import jsonpatch

from civ_agent.protocol import JsonObject

INSTRUCTIONS = (
    "A stateUpdate delta may include patch: apply its JSON Patch operations to the overview after set/collections. "
    "Detail reads are always revalidated. Keep each query's data and detailUpdate metadata. "
    "kind=full supplies data; unchanged preserves it; delta applies the JSON Patch operations to data. "
    "JSON Patch uses / paths (escape ~ as ~0 and / as ~1), zero-based array indices, and add/remove/replace/move/copy/test. "
    "Apply operations in order. scopeId and baseStateId must match; ignore duplicate/older sequence numbers. "
    "detailUpdate.baseQuery, when present, names a different delivered query whose data is the base. "
    "Apply the update into the current query's data without changing the base query. "
    "A new scope requires full data. If a base is missing, repeat that detail read with delivery=full. "
    "An unchanged reply still supplies current evidence and confirms the inspection; it does not permit stale actions. "
    "memory_update can return recordUpdate instead of echoing record. base=submitted starts from this edit's content; "
    "base=record starts from the last complete record with that ID in this conversation. Apply set replacements and "
    "removed fields, keeping all other fields. The receipt's version is authoritative; ignore older versions. "
    "If the record base is missing, use memory_read with delivery=full. Rejected edits are never applied."
)


def encoded(value) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def exact_patch(before, after) -> list[JsonObject] | None:
    """Use canonical object ordering and reject patches that conflate JSON types."""
    before, after = json.loads(encoded(before)), json.loads(encoded(after))
    patch = jsonpatch.make_patch(before, after).patch
    if encoded(jsonpatch.apply_patch(before, patch)) != encoded(after):
        return None
    return patch


def update_value(
    previous: JsonObject | None, value: JsonObject, scope: str, sequence: int, identity: str, *, full=False
):
    state_id = "d" + hashlib.sha256(encoded([scope, identity, value]).encode()).hexdigest()[:24]
    metadata: JsonObject = {"kind": "full", "scopeId": scope, "sequence": sequence, "stateId": state_id}
    if not full and previous is not None and previous["scopeId"] == scope and previous["sequence"] < sequence:
        candidate = {**metadata, "baseStateId": previous["stateId"]}
        if encoded(previous["value"]) == encoded(value):
            candidate["kind"] = "unchanged"
        else:
            patch = exact_patch(previous["value"], value)
            if patch is not None:
                candidate.update(kind="delta", patch=patch)
        if candidate["kind"] != "full" and len(encoded({"detailUpdate": candidate})) < len(
            encoded({"detailUpdate": metadata, "data": value})
        ):
            metadata = candidate
    return metadata


def apply_value(previous: JsonObject | None, metadata: JsonObject, value: JsonObject | None = None) -> JsonObject:
    if previous is not None and previous["scopeId"] == metadata["scopeId"]:
        if metadata["sequence"] <= previous["sequence"]:
            return copy.deepcopy(previous)
    if metadata["kind"] == "full":
        if value is None:
            raise ValueError("FULL_PAYLOAD_REQUIRED")
        current = value
    else:
        if (
            previous is None
            or previous["scopeId"] != metadata["scopeId"]
            or previous["stateId"] != metadata["baseStateId"]
        ):
            raise ValueError("FULL_PAYLOAD_REQUIRED")
        current = previous["value"]
        if metadata["kind"] == "delta":
            current = jsonpatch.apply_patch(current, metadata["patch"])
        elif metadata["kind"] != "unchanged":
            raise ValueError("UNKNOWN_PAYLOAD_UPDATE")
    return {**copy.deepcopy(metadata), "value": copy.deepcopy(current)}


def detail_response(
    previous: JsonObject | None,
    result: JsonObject,
    scope: str,
    sequence: int,
    identity: str,
    *,
    full=False,
    baselines: dict[str, JsonObject] | None = None,
):
    if not isinstance(result.get("data"), dict):
        return result
    metadata = update_value(previous, result["data"], scope, sequence, identity, full=full)
    response = {
        **{k: copy.deepcopy(v) for k, v in result.items() if k != "data"},
        **({"data": copy.deepcopy(result["data"])} if metadata["kind"] == "full" else {}),
        "detailUpdate": metadata,
    }
    if full or not baselines or "query" not in result:
        return response
    query = normalized_detail_query(result["query"])
    for baseline in sorted(baselines.values(), key=lambda value: encoded(value.get("query"))):
        source_query = baseline.get("query")
        if (
            baseline.get("scopeId") != scope
            or source_query is None
            or not compatible_detail_queries(source_query, query)
            or encoded(source_query) == encoded(query)
        ):
            continue
        alternative = detail_response(baseline, result, scope, sequence, identity)
        if alternative["detailUpdate"]["kind"] == "full":
            continue
        alternative["detailUpdate"]["baseQuery"] = copy.deepcopy(source_query)
        if len(encoded(alternative)) < len(encoded(response)):
            response = alternative
    return response


def normalized_detail_query(query: JsonObject) -> JsonObject:
    # Local imports keep transport primitives usable by the boundary schemas.
    from civ_agent.observations import DETAIL_MODELS, DetailQuery

    parsed = DetailQuery.model_validate(query)
    model = DETAIL_MODELS.get(parsed.tool)
    if model is None:
        raise ValueError("INVALID_DETAIL_QUERY")
    arguments = model.model_validate(parsed.arguments).model_dump(exclude_unset=True, exclude={"full", "delivery"})
    return {"tool": parsed.tool, "arguments": arguments}


def compatible_detail_queries(before: JsonObject, after: JsonObject) -> bool:
    from civ_agent.observations import DETAIL_MODELS

    tool = after["tool"]
    if tool != before.get("tool") or tool not in {"city_details", "unit_details", "construction_details"}:
        return False
    different = "names" if tool == "construction_details" else "sections"
    model = DETAIL_MODELS[tool]
    a = model.model_validate(before["arguments"]).model_dump(exclude={"full", "delivery", different})
    b = model.model_validate(after["arguments"]).model_dump(exclude={"full", "delivery", different})
    return encoded(a) == encoded(b)


def apply_detail_response(baselines: dict[str, JsonObject], result: JsonObject) -> JsonObject:
    """Reconstruct one destination query; never mutate a cross-query source."""
    from civ_agent.observations import DetailQuery, detail_scope
    from civ_agent.response_compaction import expand_response

    result = expand_response(result)

    query = normalized_detail_query(result["query"])
    key = detail_scope(DetailQuery.model_validate(query))
    metadata = result["detailUpdate"]
    destination = baselines.get(key)
    if destination is not None and destination["scopeId"] == metadata["scopeId"]:
        if metadata["sequence"] <= destination["sequence"]:
            return copy.deepcopy(destination)
    source = destination
    if metadata.get("baseQuery") is not None:
        source_query = normalized_detail_query(metadata["baseQuery"])
        if not compatible_detail_queries(source_query, query):
            raise ValueError("FULL_PAYLOAD_REQUIRED: incompatible baseQuery")
        source = baselines.get(detail_scope(DetailQuery.model_validate(source_query)))
    if metadata["kind"] != "full" and (
        source is None
        or source["scopeId"] != metadata["scopeId"]
        or source["stateId"] != metadata.get("baseStateId")
        or source["sequence"] >= metadata["sequence"]
    ):
        raise ValueError("FULL_PAYLOAD_REQUIRED")
    # A full destination does not depend on the source, even if that source has
    # advanced. Do not let apply_value's replay guard return a different query.
    state = apply_value(None if metadata["kind"] == "full" else source, metadata, result.get("data"))
    expected = update_value(None, state["value"], metadata["scopeId"], metadata["sequence"], key)["stateId"]
    if state["stateId"] != expected:
        raise ValueError("FULL_PAYLOAD_REQUIRED: detail state mismatch")
    return {**state, "query": query}


def compact_memory_receipt(result: JsonObject, changes: list[JsonObject], records: dict[str, JsonObject]) -> JsonObject:
    compact = copy.deepcopy(result)
    baselines = copy.deepcopy(records)
    for request, change in zip(changes, compact.get("changes", []), strict=True):
        record = change.get("record")
        if not change.get("accepted") or not isinstance(record, dict):
            continue
        submitted = request.get("content") if request.get("op") in {"create", "replace"} else None
        previous = submitted if submitted is not None else baselines.get(change["id"])
        baselines[change["id"]] = copy.deepcopy(record)
        if previous is None:
            continue
        update = {
            "base": "submitted" if submitted is not None else "record",
            "set": {
                k: copy.deepcopy(v)
                for k, v in record.items()
                if k not in previous or encoded(previous[k]) != encoded(v)
            },
            "removed": sorted(previous.keys() - record.keys()),
        }
        if len(encoded(update)) < len(encoded(record)):
            change.pop("record")
            change["recordUpdate"] = update
    return compact


def apply_memory_receipt(changes: list[JsonObject], result: JsonObject, records: dict[str, JsonObject]) -> JsonObject:
    """Reconstruct authoritative saved records, including defaults and metadata."""
    expanded = copy.deepcopy(result)
    baselines = copy.deepcopy(records)
    for request, change in zip(changes, expanded.get("changes", []), strict=True):
        update = change.pop("recordUpdate", None)
        if update is None:
            if change.get("accepted") and isinstance(change.get("record"), dict):
                baselines[change["id"]] = copy.deepcopy(change["record"])
            continue
        previous = request.get("content") if update["base"] == "submitted" else baselines.get(change["id"])
        if previous is None:
            raise ValueError("FULL_RECORD_REQUIRED")
        record = copy.deepcopy(previous)
        for key in update["removed"]:
            record.pop(key, None)
        record.update(copy.deepcopy(update["set"]))
        change["record"] = record
        baselines[change["id"]] = copy.deepcopy(record)
    return expanded
