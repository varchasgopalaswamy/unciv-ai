"""Cheap, player-visible economy wakeups and checkpointed review baselines."""

import hashlib
import json

from civ_agent.memory_store import RecordStore, expired_at
from civ_agent.protocol import JsonObject
from civ_agent.unit_ownership import unit_owner
from civ_agent.war_state import war_opponents


def _position(tile):
    position = tile.get("position") or {}
    return f"{position.get('x', 0)},{position.get('y', 0)}"


def worker_facts(view, state):
    """Track decisions and visible danger, not ordinary job countdowns."""
    result = {}
    for unit in (view.get("map") or {}).get("units", view.get("units", [])):
        if unit_owner(unit, state) != "economy":
            continue
        worker = unit.get("worker") or {}
        destination = (unit.get("movementOrder") or {}).get("destination")
        order = ((unit.get("combat") or {}).get("order") or {}).get("action")
        result[unit["handle"]] = {
            "job": worker.get("currentJob"),
            "queue": [step["name"] for step in worker.get("queue", [])],
            "destination": destination,
            "position": unit.get("position"),
            "health": unit.get("health"),
            "needsOrders": not worker.get("currentJob")
            and not destination
            and not order
            and float(str(unit.get("movement", "0")).partition("/")[0]) > 0,
            "nearbyHostiles": unit.get("nearbyHostiles", []),
        }
    return result


def economic_facts(view, plans, interval, state=None):
    """Omit clocks, stored work and ordinary treasury growth."""
    economy = view.get("economy") or {}
    income = economy.get("goldPerTurn", 0)
    gold = economy.get("availableGold", economy.get("treasury", 0))
    happiness = economy.get("happiness", 0)
    cities = {}
    for city in view.get("cities", []):
        development, management = city.get("development") or {}, city.get("management") or {}
        targets = plans.get(city["handle"], {}).get("purchaseTargets", [])
        purchases = (city.get("purchases") or {}).get("constructions", [])
        cities[city["handle"]] = {
            "name": city.get("name", city["handle"]),
            "puppet": city.get("puppet", False),
            "population": development.get("population"),
            "unassigned": development.get("unassignedPopulation"),
            "starving": development.get("starving", False),
            "resistance": development.get("inResistance", False),
            "focus": development.get("focus"),
            "avoidGrowth": development.get("avoidGrowth", False),
            "manualSpecialists": management.get("manualSpecialists", False),
            "queue": city.get("constructionQueue", []),
            "chosenTurn": (city.get("constructionChoice") or {}).get("chosenTurn"),
            "buildings": sorted(development.get("builtBuildings", [])),
            "stalled": any(
                row.get("index") == 0
                and not row.get("perpetual")
                and isinstance(row.get("productionPerTurn"), (int, float))
                and row["productionPerTurn"] <= 0
                for row in development.get("constructions", [])
            ),
            "options": sorted(city.get("constructionOptions", [])),
            "constructionTerms": [
                {key: row.get(key) for key in ("name", "productionCost", "productionPerTurn")}
                for row in development.get("constructions", [])
            ],
            "specialists": sorted(
                [s["name"], s.get("slots", 0), s.get("assigned", 0)] for s in management.get("specialists", [])
            ),
            "tiles": {
                _position(tile): {key: tile.get(key) for key in ("yields", "worked", "locked", "blockaded", "canWork")}
                for tile in development.get("tiles", [])
            },
            "purchases": {name: any(p["name"] == name and p.get("available") for p in purchases) for name in targets},
        }
    facts: JsonObject = {
        "cities": cities,
        "workers": worker_facts(view, state or {}),
        # These are wakeup bands, never gameplay restrictions or calculations.
        "happinessBand": sum(happiness < threshold for threshold in (0, -10, -20)),
        "deficit": income < 0,
        "cashRunwayBand": (0 if gold <= 0 else 1 if gold <= -income * interval else 2) if income < 0 else 2,
        "resources": {
            r["name"]: {key: r.get(key) for key in ("available", "imported", "exported", "spendable")}
            for r in economy.get("resources", [])
        },
        "connections": sorted([c["name"], c.get("connectedToCapital")] for c in economy.get("cityConnections", [])),
        "goldenAge": (economy.get("goldenAge") or {}).get("turnsRemaining", 0) > 0,
        "policies": sorted(
            policy for branch in (view.get("culture") or {}).get("branches", []) for policy in branch.get("adopted", [])
        ),
        "technologies": sorted(
            tech["name"] for tech in (view.get("research") or {}).get("technologies", []) if tech.get("researched")
        ),
        "wars": sorted(war_opponents(view)),
    }
    discoveries = hashlib.sha256(json.dumps([facts["policies"], facts["technologies"]]).encode()).hexdigest()
    for city in cities.values():
        city["discoveryState"] = discoveries
    return facts


def wake_reasons(previous, current):
    reasons = []
    old_cities, cities = previous["cities"], current["cities"]
    for handle in sorted(cities.keys() - old_cities.keys()):
        reasons.append(f"Newly owned city: {cities[handle]['name']}")
    for handle in sorted(old_cities.keys() - cities.keys()):
        reasons.append(f"City lost: {old_cities[handle]['name']}")
    for handle in sorted(cities.keys() & old_cities.keys()):
        old, city = old_cities[handle], cities[handle]
        for keys, message in (
            (("puppet", "resistance", "focus", "avoidGrowth", "manualSpecialists"), "City control changed"),
            (("population", "unassigned", "starving"), "Population or food needs changed"),
            (("queue", "chosenTurn", "buildings", "stalled"), "Construction changed or completed"),
            (("options", "specialists"), "Economic options changed"),
            (("tiles",), "Workable tiles changed"),
        ):
            if any(old.get(key) != city.get(key) for key in keys):
                reasons.append(f"{message}: {city['name']}")
        if any(available and not old.get("purchases", {}).get(name) for name, available in city["purchases"].items()):
            reasons.append(f"Planned purchase affordable: {city['name']}")
    for key, message in (
        ("happinessBand", "Happiness crossed a review threshold"),
        ("deficit", "Gold income entered or left deficit"),
        ("cashRunwayBand", "Treasury runway changed"),
        ("resources", "Resource access changed"),
        ("connections", "City connections changed"),
        ("goldenAge", "Golden age started or ended"),
        ("policies", "Adopted policies changed"),
        ("technologies", "Technology completed"),
        ("wars", "War started or ended"),
    ):
        if previous.get(key) != current.get(key):
            reasons.append(message)
    return reasons


class EconomySchedule:
    """State lives in private memory meta, so checkpoint/rewind restores it too."""

    def __init__(self, memory, interval):
        self.memory, self.interval = memory, interval

    def _plans(self, db, turn):
        return {
            record["body"]["city"]: record["body"]
            for record in RecordStore(db).all(categories={"city_plan"}, project=False).values()
            if record["body"].get("state") != "retired" and not expired_at(record, turn)
        }

    def assess(self, view, turn, requests):
        with self.memory.transaction() as db:
            saved = self.memory._meta(db, "economy:schedule", {})
            plans = self._plans(db, turn)
            state = self.memory._meta(db, "war:state", {})
        last = saved.get("lastReviewTurn")
        full_turn = saved.get("lastFullReviewTurn", last)
        due = full_turn + self.interval if full_turn is not None else None
        facts = economic_facts(view, plans, self.interval, state)
        baseline = saved.get("baseline") or {}
        old_cities = baseline.get("cities", {})
        reviewed = saved.get("cityReviewTurns", {})
        reasons, selected = [], set()
        pending = wake_reasons(baseline, facts) if baseline else []
        dirty = {handle for handle, city in facts["cities"].items() if city != old_cities.get(handle)}
        full = False
        worker_turn = saved.get("workerReviewTurn")
        previous_workers = baseline.get("workers", {})
        for handle, worker in facts["workers"].items():
            old = previous_workers.get(handle)
            if old is None:
                # A newly captured, exhausted worker cannot take orders yet.
                # It will be assessed again next turn, even without a review.
                if (
                    worker["needsOrders"]
                    or worker["nearbyHostiles"]
                    or (worker["health"] is not None and worker["health"] < 100)
                ):
                    reasons.append("New worker under Economy control: " + handle)
            elif worker["needsOrders"] and (worker_turn != turn or worker != old):
                reasons.append("Worker needs orders: " + handle)
            elif (old.get("job") and not worker["job"]) or (old.get("destination") and not worker["destination"]):
                reasons.append("Worker job or movement completed or interrupted: " + handle)
            elif worker["health"] != old.get("health") or worker["nearbyHostiles"] != old.get("nearbyHostiles", []):
                reasons.append("Worker health or visible threats changed: " + handle)
        if previous_workers.keys() - facts["workers"].keys():
            reasons.append("Worker lost or reassigned")
        if facts["cities"]:
            if not baseline or full_turn is None:
                reasons.append("First economy review")
                full = True
            elif due is not None and turn >= due:
                reasons.append("Periodic economy review due")
                full = True
            for key, message in (
                ("happinessBand", "Happiness crossed a review threshold"),
                ("deficit", "Gold income entered or left deficit"),
                ("cashRunwayBand", "Treasury runway changed"),
                ("resources", "Resource access changed"),
                ("wars", "War started or ended"),
            ):
                if baseline and baseline.get(key) != facts[key]:
                    reasons.append(message)
                    full = True
            for handle, city in facts["cities"].items():
                old = old_cities.get(handle, {})
                city_turn = reviewed.get(handle, last if handle in old_cities else None)

                def add(message, handle=handle, name=city["name"]):
                    selected.add(handle)
                    reasons.append(f"{message}: {name}")

                if handle not in old_cities:
                    add("Newly owned city")
                if not city["puppet"] and handle not in plans and city_turn != turn:
                    add("Missing city plan")
                if not city["puppet"] and not city["resistance"]:
                    # These schedule attention; leaving a native-legal position
                    # unchanged remains a valid decision, acknowledged once.
                    for condition, keys, message in (
                        (
                            not city["queue"] or city["queue"] == ["Nothing"],
                            ("queue", "chosenTurn"),
                            "Construction needs a choice",
                        ),
                        (bool(city["unassigned"]), ("unassigned",), "Unassigned citizens need review"),
                        (city["starving"], ("starving",), "Food deficit needs review"),
                        (city["stalled"], ("stalled",), "Stalled construction needs review"),
                    ):
                        if condition and (city_turn != turn or any(old.get(key) != city[key] for key in keys)):
                            add(message)
                if old and (
                    old.get("options") != city["options"]
                    or [s[:2] for s in old.get("specialists", [])] != [s[:2] for s in city["specialists"]]
                    or "discoveryState" in old
                    and "constructionTerms" in old
                    and old["discoveryState"] != city["discoveryState"]
                    and old["constructionTerms"] != city["constructionTerms"]
                ):
                    add("Economic options changed")
                if any(
                    available and not old.get("purchases", {}).get(name)
                    for name, available in city["purchases"].items()
                ):
                    add("Planned purchase affordable")
                date = plans.get(handle, {}).get("reviewTurn")
                if date is not None and date <= turn and (city_turn is None or city_turn < date):
                    add("Planned review due")
            for request in requests:
                urgent = request["status"] == "pending" and request.get("priority") == "urgent"
                deferred = (
                    request["status"] == "deferred"
                    and request.get("reviewTurn") is not None
                    and request["reviewTurn"] <= turn
                    and request.get("reviewedTurn", -1) < request["reviewTurn"]
                )
                if (urgent or deferred) and request.get("reviewedTurn") != turn:
                    reasons.append("Economic request requires review: " + request["id"])
                    target = request_city(view, request.get("city"))
                    if target is None:
                        full = True
                    else:
                        selected.add(target)
                elif request["status"] == "pending":
                    pending.append("Routine economic request: " + request["id"])
            if full:
                selected = set(facts["cities"])
        return {
            "lastReviewTurn": last,
            "lastFullReviewTurn": full_turn,
            "workerReviewTurn": worker_turn,
            "workers": sorted(facts["workers"]),
            "cityReviewTurns": {
                handle: reviewed.get(handle, last if handle in old_cities else None) for handle in facts["cities"]
            },
            "nextReviewTurn": due if facts["cities"] else None,
            "reasons": list(dict.fromkeys(reasons)),
            "cities": sorted(selected),
            "fullReview": full,
            "pendingReasons": list(dict.fromkeys(pending)),
            "pendingCities": sorted(dirty),
        }

    def complete(self, view, turn, observed=None, *, cities=None, full=True):
        with self.memory.transaction() as db:
            if self.memory._meta(db, "epoch") != view.get("epoch"):
                raise ValueError("STALE_EPOCH")
            plans = self._plans(db, turn)
            state = self.memory._meta(db, "war:state", {})
            facts = economic_facts(view, plans, self.interval, state)
            selected = set(facts["cities"]) if full else set(cities or [])
            if observed is not None:
                delivered = economic_facts(observed, plans, self.interval, state)
                if full and facts != delivered:
                    return False
                if not full and (
                    {k: v for k, v in facts.items() if k != "cities"}
                    != {k: v for k, v in delivered.items() if k != "cities"}
                    or any(facts["cities"].get(city) != delivered["cities"].get(city) for city in selected)
                ):
                    return False
            saved = self.memory._meta(db, "economy:schedule", {})
            previous = saved.get("baseline") or {}
            reviewed = saved.get(
                "cityReviewTurns", {city: saved.get("lastReviewTurn") for city in previous.get("cities", {})}
            )
            acknowledged = dict(previous.get("cities", {}))
            for city in selected:
                if city in facts["cities"]:
                    acknowledged[city] = facts["cities"][city]
                    reviewed[city] = turn
                else:
                    acknowledged.pop(city, None)
                    reviewed.pop(city, None)
            if full:
                acknowledged = facts["cities"]
                reviewed = {city: turn for city in acknowledged}
            self.memory._set(
                db,
                "economy:schedule",
                {
                    "lastReviewTurn": turn,
                    "lastFullReviewTurn": turn
                    if full
                    else saved.get("lastFullReviewTurn", saved.get("lastReviewTurn")),
                    "cityReviewTurns": reviewed,
                    "workerReviewTurn": turn,
                    "baseline": facts if full else {**previous, "cities": acknowledged, "workers": facts["workers"]},
                },
            )
            return True


def request_city(view, reference):
    """Resolve an exact handle or unambiguous native display name; prose stays prose."""
    if reference is None:
        return None
    cities = view.get("cities", [])
    if reference in {city["handle"] for city in cities}:
        return reference
    matches = [city["handle"] for city in cities if city.get("name") == reference]
    return matches[0] if len(matches) == 1 else None
