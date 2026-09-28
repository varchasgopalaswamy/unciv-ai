"""Small comparisons of owned/visible facts; no inferred enemy actions or intentions."""

import copy

from civ_agent.protocol import JsonObject


def turn_basis(snapshot: JsonObject) -> JsonObject:
    return copy.deepcopy(
        {
            "turn": snapshot["turn"],
            "units": {
                unit["handle"]: {
                    "name": unit["name"],
                    "position": unit["position"],
                    "job": (unit.get("worker") or {}).get("currentJob"),
                    "destination": (unit.get("movementOrder") or {}).get("destination"),
                    "order": ((unit.get("combat") or {}).get("order") or {}).get("action"),
                }
                for unit in snapshot.get("map", {}).get("units", [])
            },
            "cities": {
                city["handle"]: {
                    "name": city["name"],
                    "position": (city.get("development") or {}).get("position"),
                    "constructionOptions": city.get("constructionOptions", []),
                }
                for city in snapshot.get("cities", [])
            },
            "notifications": [notice["handle"] for notice in snapshot.get("notifications", [])],
            "reports": [report["id"] for report in snapshot.get("combatReports", [])],
            "resources": {
                resource["name"]: resource["available"]
                for resource in (snapshot.get("economy") or {}).get("resources", [])
            },
            "goldenAge": ((snapshot.get("economy") or {}).get("goldenAge") or {}).get("turnsRemaining", 0),
        }
    )


def turn_changes(before: JsonObject, snapshot: JsonObject) -> JsonObject:
    after = turn_basis(snapshot)
    result: JsonObject = {"fromTurn": before["turn"]}
    for key in ("notifications", "reports"):
        added = [handle for handle in after[key] if handle not in before[key]]
        if added:
            result[key] = added  # Contents already live in notifications/combatReports.
    options = [
        {
            "city": handle,
            "names": sorted(set(city["constructionOptions"]) - set(before["cities"][handle]["constructionOptions"])),
        }
        for handle, city in after["cities"].items()
        if handle in before["cities"]
    ]
    if any(row["names"] for row in options):
        result["newConstructionOptions"] = [row for row in options if row["names"]]
    for collection in ("units", "cities"):
        removed = sorted(before[collection].keys() - after[collection].keys())
        if removed:
            # Historical identity only: consumption, gifting and capture can all remove ownership.
            result[collection + "NoLongerOwned"] = [
                {
                    "handle": handle,
                    "name": before[collection][handle]["name"],
                    "lastKnownPosition": before[collection][handle]["position"],
                }
                for handle in removed
            ]
    jobs, orders = [], []
    tiles = {(tile["position"]["x"], tile["position"]["y"]): tile for tile in snapshot.get("map", {}).get("tiles", [])}
    for handle, unit in after["units"].items():
        old = before["units"].get(handle)
        if not old:
            continue
        if old["job"] and (old["job"] != unit["job"] or old["position"] != unit["position"]):
            tile = tiles.get((old["position"]["x"], old["position"]["y"]), {})
            completed = tile.get("visible") and (
                tile.get("improvement") == old["job"]
                and not tile.get("improvementPillaged")
                or tile.get("road") == old["job"]
                and not tile.get("roadPillaged")
            )
            jobs.append(
                {
                    "unit": handle,
                    "job": old["job"],
                    "outcome": "COMPLETED" if completed else "ENDED",
                    "position": old["position"],
                }
            )
        if old["destination"] and old["destination"] != unit["destination"]:
            orders.append(
                {
                    "unit": handle,
                    "destination": old["destination"],
                    "outcome": "ARRIVED" if unit["position"] == old["destination"] else "INTERRUPTED",
                }
            )
        elif old["order"] and not unit["order"]:
            orders.append({"unit": handle, "order": old["order"], "outcome": "ENDED"})
    if jobs:
        result["workerJobs"] = jobs
    if orders:
        result["orders"] = orders
    lost_resources = sorted(
        name for name, amount in before["resources"].items() if amount > 0 and after["resources"].get(name, 0) <= 0
    )
    if lost_resources:
        result["resourcesNoLongerAvailable"] = lost_resources
    if before["goldenAge"] > 0 and after["goldenAge"] == 0:
        result["goldenAgeEnded"] = True
    elif 0 < after["goldenAge"] <= 2:
        result["goldenAgeEndingInTurns"] = after["goldenAge"]
    expiring = [
        agreement["agreement"]
        for agreement in (snapshot.get("communication") or {}).get("agreements", [])
        if agreement.get("status") == "ACTIVE" and 0 < (agreement.get("remainingTurns") or 0) <= 2
    ]
    if expiring:
        result["agreementsExpiring"] = expiring  # Full terms/expiry are already in chat_read.
    return copy.deepcopy(result)
