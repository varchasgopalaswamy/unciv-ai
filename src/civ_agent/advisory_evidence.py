"""Bounded comparisons of recorded facts, without inferring omitted information."""

import copy
from typing import Any

from civ_agent.decisions_ledger import canonical, fingerprint
from civ_agent.protocol import JsonObject

MATERIAL_SCHEMA = "recorded-material-v1"


def _unknown_fields(view):
    economy = view.get("economy") or {}
    progress = view.get("recentProgress") or {}
    diplomacy = view.get("diplomacy")
    recorded = {
        "cities": isinstance(view.get("cities"), list),
        "units": isinstance(view.get("units"), list) or isinstance((view.get("map") or {}).get("units"), list),
        "researchQueue": "researchQueue" in view,
        "diplomacy": isinstance(diplomacy, list)
        or isinstance(diplomacy, dict)
        and isinstance(diplomacy.get("knownCivilizations"), list),
        "economicPressure": all(key in economy for key in ("happiness", "goldPerTurn")),
        "resources": isinstance(economy.get("resources"), list),
        "stalledProduction": isinstance(progress.get("cities"), list),
        "stalledOrders": isinstance(progress.get("units"), list),
        "hostiles": isinstance((view.get("tacticalReview") or {}).get("visibleHostiles"), list),
    }
    paths = []
    for index, city in enumerate(view.get("cities", [])):
        for keys, field in (
            (("handle",), "handle"),
            (("name",), "name"),
            (("constructionQueue",), "constructionQueue"),
            (("health", "maxHealth"), "damaged"),
            (("constructionChoice",), "constructionChoice"),
        ):
            if not all(key in city for key in keys):
                paths.append(f"cities[{index}].{field}")
    units = view.get("units", (view.get("map") or {}).get("units", []))
    for index, unit in enumerate(units):
        if "workerJob" not in unit and "currentJob" not in (unit.get("worker") or {}):
            paths.append(f"units[{index}].workerJob")
    unknown = {key for key, known in recorded.items() if not known}
    unknown.update(path.split("[", 1)[0] for path in paths)
    return sorted(unknown), paths


def bounded_facts(view, *, list_limit=64) -> JsonObject:
    from civ_agent.advisory_store import material_facts

    if type(list_limit) is not int or list_limit < 1:
        raise ValueError("Material list limit must be a positive integer")
    facts = material_facts(view)
    omitted = {}
    unknown, unknown_paths = _unknown_fields(view)

    def bound(value, path) -> Any:
        if isinstance(value, (list, tuple)):
            if len(value) > list_limit:
                omitted[path] = len(value) - list_limit
            return [bound(item, f"{path}[{index}]") for index, item in enumerate(value[:list_limit])]
        if isinstance(value, dict):
            return {key: bound(item, f"{path}.{key}" if path else key) for key, item in value.items()}
        return copy.deepcopy(value)

    return {
        "schema": MATERIAL_SCHEMA,
        "facts": bound(facts, ""),
        "fullFingerprint": fingerprint(facts),
        "coverage": {
            "listLimit": list_limit,
            "omitted": omitted,
            "unknownFields": unknown,
            "unknownPaths": unknown_paths[:list_limit],
            "omittedUnknownPaths": max(0, len(unknown_paths) - list_limit),
        },
    }


def material_changes(before, current) -> JsonObject:
    """Changes within matching projections; truncated excerpts never establish absence."""
    if before is None or before.get("schema") != current.get("schema"):
        return {"baselineAvailable": False, "comparisonComplete": False, "fields": []}
    omitted = [*before.get("coverage", {}).get("omitted", {}), *current.get("coverage", {}).get("omitted", {})]
    unknown = {
        field for projection in (before, current) for field in projection.get("coverage", {}).get("unknownFields", [])
    }
    unknown_paths = [
        path for projection in (before, current) for path in projection.get("coverage", {}).get("unknownPaths", [])
    ]
    fields = []
    for key in sorted(set(before["facts"]) | set(current["facts"])):
        previous, latest = before["facts"].get(key), current["facts"].get(key)
        if canonical(previous) == canonical(latest):
            continue
        complete = key not in unknown and not any(
            path == key or path.startswith((key + "[", key + ".")) for path in [*omitted, *unknown_paths]
        )
        if not complete:
            fields.append({"field": key, "complete": False, "beforeExcerpt": previous, "currentExcerpt": latest})
        elif isinstance(previous, list) and isinstance(latest, list):
            old = {canonical(item): item for item in previous}
            new = {canonical(item): item for item in latest}
            fields.append(
                {
                    "field": key,
                    "complete": True,
                    "removed": [value for item, value in old.items() if item not in new],
                    "added": [value for item, value in new.items() if item not in old],
                }
            )
        else:
            fields.append({"field": key, "complete": True, "before": previous, "current": latest})
    return {
        "baselineAvailable": True,
        "fullFactsChanged": before["fullFingerprint"] != current["fullFingerprint"],
        "comparisonComplete": not (omitted or unknown or unknown_paths),
        "fields": fields,
    }
