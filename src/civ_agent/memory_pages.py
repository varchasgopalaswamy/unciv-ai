"""Bound notebook delivery without truncating records or changing their versions."""

import copy

from jsonpointer import EndOfList, JsonPointer, JsonPointerException

from civ_agent.protocol import JsonObject
from civ_agent.strict_schema import encoded

# Leave room for the rest of a multi-read batch and its delivery metadata.
READ_BUDGET = 48 * 1024
VALUE_BUDGET = 2048
PAGE_ITEMS = 20


def bounded_records(result: JsonObject, versions: dict[str, int]) -> JsonObject:
    response = dict(result)
    records, pages = [], []
    remaining = READ_BUDGET
    for record in response.get("records", []):
        serialized = encoded(record)
        size = len(serialized.encode())
        if size <= remaining:
            records.append(record)
            remaining -= size
        else:
            source = record.get("evidence", record["id"])
            pages.append(
                {
                    "id": source,
                    "recordId": record["id"],
                    "type": record["type"],
                    "version": versions.get(source),
                    "characters": len(serialized),
                    "path": "",
                }
            )
    response["records"] = records
    if pages:
        response["recordPages"] = pages
    return response


def value_type(value):
    if isinstance(value, dict):
        return "object"
    if isinstance(value, list):
        return "array"
    if isinstance(value, str):
        return "string"
    return "scalar"


def memory_page(record: JsonObject, *, id: str, version: int, path: str = "", offset: int = 0) -> JsonObject:
    """JSON Pointer navigation plus stable child/string pagination, without summaries."""
    try:
        pointer = JsonPointer(path)
        value = pointer.resolve(record)
    except (JsonPointerException, TypeError, IndexError) as error:
        raise ValueError("INVALID_MEMORY_PATH") from error
    if isinstance(value, EndOfList):
        raise ValueError("INVALID_MEMORY_PATH")
    result = {"id": id, "version": version, "path": path, "offset": offset, "kind": value_type(value)}
    if isinstance(value, str):
        # Bound even escaped control characters (six JSON characters per codepoint).
        end = min(len(value), offset + VALUE_BUDGET // 6)
        result.update(value=value[offset:end], hasMore=end < len(value), nextOffset=end, total=len(value))
    elif isinstance(value, (dict, list)):
        keys = sorted(value) if isinstance(value, dict) else list(range(len(value)))
        entries = []
        for key in keys[offset : offset + PAGE_ITEMS]:
            child = value[key]
            child_path = JsonPointer.from_parts([*pointer.parts, str(key)]).path
            serialized = encoded(child)
            entry = {"path": child_path, "kind": value_type(child), "characters": len(serialized)}
            if len(serialized.encode()) <= VALUE_BUDGET:
                entry["value"] = copy.deepcopy(child)
            else:
                entry["pageable"] = True
                entry["length"] = len(child)
            entries.append(entry)
        end = min(len(keys), offset + len(entries))
        result.update(entries=entries, hasMore=end < len(keys), nextOffset=end, total=len(keys))
    else:
        result.update(value=value, hasMore=False, nextOffset=0, total=1)
    return result
