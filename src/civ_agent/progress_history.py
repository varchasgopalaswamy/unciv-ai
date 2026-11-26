"""Bounded observations and receipt accounting. Strategic meaning belongs to the player."""

import copy
import json
from collections import Counter

from civ_agent.protocol import JsonObject
from civ_agent.review_context import number

HISTORY_TURNS = 20
MAX_ROWS = 12


def position(snapshot: JsonObject) -> JsonObject:
    economy = snapshot.get("economy") or {}
    return {
        "turn": snapshot["turn"],
        "exploredTiles": len(snapshot["map"].get("tiles", [])) if "map" in snapshot else None,
        "happiness": number(economy.get("happiness")),
        "unitMaintenance": number(economy.get("unitMaintenance")),
        "units": {
            unit["handle"]: {
                "name": unit["name"],
                "position": unit["position"],
                "order": ((unit.get("combat") or {}).get("order") or {}).get("action"),
                "destination": (unit.get("movementOrder") or {}).get("destination"),
                "military": unit.get("military", False),
            }
            for unit in snapshot.get("map", {}).get("units", [])
            if unit.get("position") is not None
        },
        "cities": {
            city["handle"]: {
                "name": city["name"],
                "queue": city.get("constructionQueue", []),
                "progress": {
                    row["name"]: row.get("productionStored")
                    for row in reversed((city.get("development") or {}).get("constructions", []))
                    if not row.get("perpetual")
                },
                "foodPerTurn": (city.get("development") or {}).get("foodPerTurn"),
            }
            for city in snapshot.get("cities", [])
        },
        "resources": {
            row["name"]: {key: row.get(key) for key in ("available", "imported", "exported")}
            for row in economy.get("resources", [])
        },
        "relations": {
            (row.get("civilizationName") or row["civilizationId"]): {
                "atWar": row.get("atWar"),
                "receivedOpenBorders": any(
                    term.get("name") == "Open Borders"
                    for agreement in row.get("agreements", [])
                    for term in agreement.get("receive", [])
                ),
            }
            for row in (snapshot.get("diplomacy") or {}).get("knownCivilizations", [])
        },
        "resourceSites": {
            json.dumps(tile["position"], sort_keys=True): {
                "position": tile["position"],
                "resource": tile["resource"],
                "owner": tile.get("owner"),
                "observedTurn": tile.get("observedTurn", snapshot["turn"]),
            }
            for tile in snapshot.get("map", {}).get("tiles", [])
            if tile.get("resource") and tile.get("visible")
        },
    }


def advance_history(history: list[JsonObject], snapshot: JsonObject) -> list[JsonObject]:
    """One latest sample per turn. Repeated reads don't lengthen a streak."""
    now = position(snapshot)
    rows = copy.deepcopy([row for row in history if now["turn"] - HISTORY_TURNS <= row["turn"] <= now["turn"]])
    prior = rows[-1] if rows else None
    moved = set(prior.get("positionChanges", [])) if prior and prior["turn"] == now["turn"] else set()
    if prior:
        now["resourceSites"] = {**prior["resourceSites"], **now["resourceSites"]}
        moved.update(
            handle
            for handle, unit in now["units"].items()
            if handle in prior["units"] and unit["position"] != prior["units"][handle]["position"]
        )
    now["positionChanges"] = sorted(moved)
    if prior and prior["turn"] == now["turn"]:
        rows[-1] = now
    else:
        rows.append(now)
    return copy.deepcopy(rows)


def investment_accounting(actions: list[JsonObject]) -> JsonObject:
    confirmed = [action for action in actions if action["commandStatus"] == "confirmed"]
    economic = [action for action in confirmed if action.get("economy") is not None]
    spending = [action for action in economic if action["economy"].get("goldChange", 0) < 0]
    return {
        "confirmedActionsByKind": dict(Counter(a.get("command", {}).get("kind", "SOCIAL") for a in confirmed)),
        "goldSpent": sum(-a["economy"]["goldChange"] for a in spending),
        "goldReceived": sum(max(0, a["economy"].get("goldChange", 0)) for a in economic),
        "spendingReceipts": [
            {
                "receipt": a["receipt"],
                "turn": a["turn"],
                "command": a["command"],
                "goldSpent": -a["economy"]["goldChange"],
            }
            for a in spending[-MAX_ROWS:]
        ],
        "omittedSpendingReceipts": max(0, len(spending) - MAX_ROWS),
        "basis": "Gold totals count confirmed changes only; missing amounts are unknown. Queueing is not completion. Unit upkeep covers your whole army, not this plan alone.",
    }


def progress_context(history: list[JsonObject], actions: list[JsonObject]) -> JsonObject:
    if not history:
        return {"available": False}
    first, now = history[0], history[-1]
    result: JsonObject = {
        "fromTurn": first["turn"],
        "toTurn": now["turn"],
        "observedTurns": [row["turn"] for row in history],
        "basis": "The latest observation from each recorded turn, including movement seen during that turn. Matching positions do not prove a unit stayed still or had a route. Exploration totals cover your whole empire. Accepted orders do not prove progress; missing turns are unknown. See turn history for full details.",
    }
    if first["exploredTiles"] is not None and now["exploredTiles"] is not None:
        result["exploration"] = {
            "before": first["exploredTiles"],
            "now": now["exploredTiles"],
            "change": now["exploredTiles"] - first["exploredTiles"],
        }

    def bounded(key, rows):
        result[key] = rows[:MAX_ROWS]
        if len(rows) > MAX_ROWS:
            result.setdefault("omitted", {})[key] = len(rows) - MAX_ROWS

    groups: dict[str, JsonObject] = {}
    for action in actions:
        if action["commandStatus"] != "confirmed":
            continue
        command = action.get("command", {})
        if command.get("kind") not in {
            "SET_UNIT_ORDER",
            "SET_UNIT_DESTINATION",
            "MOVE_UNIT",
            "QUEUE_CONSTRUCTION",
            "START_CONSTRUCTION",
            "MOVE_CONSTRUCTION",
        }:
            continue
        fields = {
            k: command[k] for k in ("kind", "target", "selection", "destination", "index", "toIndex") if k in command
        }
        group = groups.setdefault(
            json.dumps(fields, sort_keys=True), {"command": fields, "count": 0, "firstTurn": action["turn"]}
        )
        group.update(count=group["count"] + 1, lastTurn=action["turn"], lastReceipt=action["receipt"])
    repeated = sorted((g for g in groups.values() if g["count"] > 1), key=lambda g: (-g["count"], -g["lastTurn"]))
    bounded("repeatedCommands", repeated)
    targets = {g["command"].get("target") for g in repeated}
    units = []
    for handle, unit in now["units"].items():
        samples = []
        for row in reversed(history):
            old = row["units"].get(handle)
            if not old or old["position"] != unit["position"]:
                break
            samples.append(row["turn"])
            if handle in row.get("positionChanges", []):
                break
        if handle in targets or unit["destination"] or unit["order"]:
            units.append(
                {
                    "unit": handle,
                    **unit,
                    "samePositionSinceObservedTurn": min(samples),
                    "samePositionObservedTurns": len(samples),
                }
            )
    bounded(
        "units", sorted(units, key=lambda u: (u["unit"] not in targets, -u["samePositionObservedTurns"], u["unit"]))
    )
    last_seen = {}
    for row in history:
        for handle, unit in row["units"].items():
            last_seen[handle] = {
                "unit": handle,
                "name": unit["name"],
                "lastKnownPosition": unit["position"],
                "lastObservedTurn": row["turn"],
            }
    bounded("unitsNoLongerOwned", [unit for handle, unit in last_seen.items() if handle not in now["units"]])
    cities = []
    for handle, city in now["cities"].items():
        samples = [(row["turn"], row["cities"][handle]) for row in history if handle in row["cities"]]
        first_turn, old = samples[0]
        retained = []
        for name, amount in city["progress"].items():
            if amount is None:
                continue
            observed = [(turn, sample["progress"].get(name)) for turn, sample in samples]
            start, before = next((turn, value) for turn, value in observed if value is not None)
            unchanged = []
            for turn, value in reversed(observed):
                if value != amount:
                    break
                unchanged.append(turn)
            retained.append(
                {
                    "construction": name,
                    "fromTurn": start,
                    "before": before,
                    "now": amount,
                    "change": amount - before,
                    "sameProductionSinceObservedTurn": min(unchanged),
                    "sameProductionObservedTurns": len(unchanged),
                    "activeNow": bool(city["queue"] and city["queue"][0] == name),
                }
            )
        cities.append(
            {
                "city": handle,
                "name": city["name"],
                "fromTurn": first_turn,
                "queueBefore": old["queue"],
                "queueNow": city["queue"],
                "retainedProduction": retained,
                "foodPerTurnBefore": old["foodPerTurn"],
                "foodPerTurnNow": city["foodPerTurn"],
            }
        )
    bounded("cities", cities)
    unhappy = []
    expected = now["turn"]
    for row in reversed(history):
        if row["turn"] != expected or row["happiness"] is None or row["happiness"] >= 0:
            break
        unhappy.append(row["turn"])
        expected -= 1
    result["happiness"] = {
        "now": now["happiness"],
        "consecutiveObservedUnhappyTurns": len(unhappy),
        "sinceTurn": min(unhappy) if unhappy else None,
        "windowLimited": bool(unhappy and len(unhappy) == len(history)),
    }
    changed = []
    for before, after in zip(history, history[1:], strict=False):
        for field in ("resources", "relations", "resourceSites"):
            for key in sorted(before[field].keys() | after[field].keys()):
                old, new = before[field].get(key), after[field].get(key)
                if old is None or new is None:
                    if old != new:
                        changed.append(
                            {
                                "fromTurn": before["turn"],
                                "toTurn": after["turn"],
                                "kind": field,
                                "subject": key,
                                "before": old,
                                "now": new,
                            }
                        )
                    continue
                if field == "resourceSites" and old["owner"] == new["owner"]:
                    continue
                if old != new:
                    changed.append(
                        {
                            "fromTurn": before["turn"],
                            "toTurn": after["turn"],
                            "kind": field,
                            "subject": key,
                            "before": old,
                            "now": new,
                        }
                    )
    bounded("changedFacts", list(reversed(changed)))
    result["investment"] = investment_accounting(actions)
    result["unlinkedInvestment"] = investment_accounting(
        [a for a in actions if not (a.get("_pinned") or {}).get("decision")]
    )
    result["unitMaintenanceNow"] = now["unitMaintenance"]
    return result
