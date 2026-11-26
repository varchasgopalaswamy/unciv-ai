"""Project replay detail panels from recorded observations and committed saves."""

import gzip
import hashlib
import json
import re
import sqlite3
from collections import defaultdict
from contextlib import closing

from civ_agent.civilization_paths import civilization_directory_name
from civ_agent.tls import regular_file


def position_key(position):
    return position.get("x", 0), position.get("y", 0)


def _city(position, name, queue, stored, cost=None, turns=None, population=None):
    return {
        "position": dict(zip(("x", "y"), position_key(position), strict=True)),
        "name": name,
        "queue": queue,
        "stored": stored,
        "cost": cost,
        "turns": turns,
        "population": population,
    }


def archived_observation(head, directory, civilization, epoch, turn=None):
    identity = head["identity"]
    if identity["scope"]["epoch"] != epoch:
        return None
    if digest := head.get("archive"):
        if not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise ValueError("Invalid notebook archive digest.")
        path = directory / "native-archives" / (digest + ".json.gz")
        if not path.exists():
            return None
        with gzip.open(regular_file(path, private=True), "rb") as file:
            raw = file.read()
        if hashlib.sha256(raw).hexdigest() != digest:
            raise ValueError(f"Notebook archive digest mismatch for {civilization}.")
        record = json.loads(raw)
        if record["identity"] != identity:
            raise ValueError(f"Notebook archive identity mismatch for {civilization}.")
    else:
        record = head
    observed = record.get("state", {}).get("data", {}).get("observation")
    if observed and (turn is None or observed.get("turn") == turn) and observed.get("map", {}).get("tiles"):
        if observed.get("civilization") not in {None, civilization} or observed.get("epoch") != epoch:
            raise ValueError(f"Observation scope mismatch for {civilization}.")
        return observed
    return None


def _observed_production(observation, civilization):
    city_positions = {
        tile["cityName"]: tile["position"]
        for tile in observation["map"]["tiles"]
        if tile.get("cityName") and tile.get("owner") == civilization
    }
    for city in observation.get("cities", []):
        development = city.get("development") or {}
        position = development.get("position", city_positions.get(city["name"]))
        if position is None:
            continue
        queue = city["constructionQueue"]
        estimates = city.get("constructionEstimates") or {}
        production = (estimates.get(queue[0], {}).get("production") or {}) if queue else {}
        yield (
            _city(
                position,
                city["name"],
                queue,
                production.get("stored"),
                production.get("cost"),
                production.get("turnsIfStartedNow"),
                development.get("population"),
            ),
            production.get("overflow"),
        )


def observed_progress(observation, civilization):
    research = observation.get("research")
    culture = observation.get("culture")
    return {
        "cities": [city for city, _ in _observed_production(observation, civilization)],
        "research": {
            "queue": observation.get("researchQueue", []),
            "known": [tech["name"] for tech in research["technologies"] if tech["researched"]],
            "available": observation.get("researchOptions", []),
            "rate": research.get("sciencePerTurn"),
            "progress": {
                tech["name"]: [tech["scienceStored"], tech["cost"], tech.get("turns")]
                for tech in research["technologies"]
                if tech["scienceStored"] or tech["name"] == research.get("current")
            },
        }
        if research is not None
        else None,
        "culture": {
            "stored": culture["storedCulture"],
            "cost": culture["nextPolicyCost"],
            "rate": culture.get("culturePerTurn"),
            "turns": culture.get("turnsToNextPolicy"),
            "available": observation.get("policyOptions", []),
            "free": observation.get("freePolicies", 0),
            "adopted": [
                name
                for branch in culture["branches"]
                for name in [
                    branch["name"],
                    *branch["adopted"],
                    *([branch["name"] + " Complete"] if branch["complete"] else []),
                ]
            ],
        }
        if culture is not None
        else None,
    }


def _production_key(city, overflow):
    return (
        position_key(city["position"]),
        city["name"],
        city["queue"][0],
        city["population"],
        city["stored"],
        overflow,
    )


def observed_construction_estimates(root, civilization, final_turn):
    """Index engine estimates by the turn and production state they describe."""
    directory = root / "coordinator" / civilization_directory_name(civilization) / "notebook"
    path = directory / "memory.sqlite3"
    if not path.exists() and not path.is_symlink():
        return {}
    regular_file(path, private=True)
    with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)) as db:
        heads = [
            head
            for (raw,) in db.execute("SELECT value FROM meta WHERE key LIKE 'nativeGatewayJob:%'")
            if "identity" in (head := json.loads(raw))
        ]
    estimates = defaultdict(lambda: defaultdict(set))
    for head in heads:
        epoch = head["identity"]["scope"]["epoch"]
        observation = archived_observation(head, directory, civilization, epoch)
        if not observation or not isinstance(turn := observation.get("turn"), int) or turn > final_turn:
            continue
        for city, overflow in _observed_production(observation, civilization):
            if city["queue"] and city["cost"] is not None:
                estimates[epoch, turn][_production_key(city, overflow)].add((city["cost"], city["turns"]))
    # The production rate can change between observations in the same turn.
    # Omit a completion estimate when the matching observations disagree.
    return {
        turn: {key: next(iter(values)) for key, values in cities.items() if len(values) == 1}
        for turn, cities in estimates.items()
    }


def committed_progress(civ, construction_estimates=None):
    cities = []
    for city in civ.get("cities", []):
        constructions = city.get("cityConstructions", {})
        queue = list(constructions.get("constructionQueue", []))
        stored = constructions.get("inProgressConstructions", {}).get(queue[0], 0) if queue else None
        item = _city(
            city.get("location", {}),
            city["name"],
            queue,
            stored,
            population=city.get("population", {}).get("population", 1),
        )
        if queue and construction_estimates:
            key = _production_key(item, constructions.get("productionOverflow", 0))
            if key in construction_estimates:
                item["cost"], item["turns"] = construction_estimates[key]
        cities.append(item)
    tech = civ.get("tech")
    policies = civ.get("policies")
    research = None
    if tech is not None:
        queue = list(tech.get("techsToResearch", []))
        progress = {name: [value] for name, value in tech.get("techsInProgress", {}).items()}
        if queue:
            progress.setdefault(queue[0], [0])
        research = {"known": list(tech.get("techsResearched", [])), "queue": queue, "progress": progress}
    return {
        "cities": cities,
        "research": research,
        "culture": {
            "adopted": list(policies.get("adoptedPolicies", [])),
            "stored": policies.get("storedCulture", 0),
            "free": policies.get("freePolicies", 0),
        }
        if policies is not None
        else None,
    }


def observed_tile_details(tile):
    return {
        key: tile[key]
        for key in (
            "naturalWonder",
            "resource",
            "resourceAmount",
            "road",
            "roadPillaged",
            "improvementPillaged",
            "improvementInProgress",
            "improvementTurns",
            "riverBottom",
            "riverBottomLeft",
            "riverBottomRight",
            "yields",
            "observedTurn",
            "cityPopulation",
            "cityHealth",
            "cityMaxHealth",
            "cityDefensiveStrength",
        )
        if (value := tile.get(key)) is not False and value not in (None, [], "None")
    }


def committed_city_details(city, building_health=None):
    details = {
        "cityPopulation": city.get("population", {}).get("population", 1),
        "cityHealth": city.get("health", 200),
    }
    buildings = city.get("cityConstructions", {}).get("builtBuildings", [])
    if building_health is not None and all(name in building_health for name in buildings):
        # City.getMaxHealth adds the built buildings' health to the base 200 HP.
        details["cityMaxHealth"] = 200 + sum(building_health[name] for name in buildings)
    return details


def committed_tile_details(tile, researched, resources, *, city=None):
    source = {
        **(city or {}),
        "naturalWonder": tile.get("naturalWonder"),
        "road": tile.get("roadStatus"),
        "roadPillaged": bool(tile.get("roadIsPillaged")),
        "improvementPillaged": bool(tile.get("improvementIsPillaged")),
        "riverBottom": bool(tile.get("hasBottomRiver")),
        "riverBottomLeft": bool(tile.get("hasBottomLeftRiver")),
        "riverBottomRight": bool(tile.get("hasBottomRightRiver")),
    }
    # An unknown resource or a strategic resource whose reveal is still locked stays private.
    resource = tile.get("resource")
    if resource in resources:
        reveal, strategic = resources[resource]
        if reveal is None or reveal in researched:
            source["resource"] = resource
            if strategic:
                source["resourceAmount"] = tile.get("resourceAmount")
    if queue := tile.get("improvementQueue"):
        first = queue[0]
        source["improvementInProgress"] = first.get("improvement", first.get("name"))
        source["improvementTurns"] = first.get("turnsToImprovement")
    return observed_tile_details(source)
