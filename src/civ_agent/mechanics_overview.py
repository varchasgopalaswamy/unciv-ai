"""Small planning summaries made only from delivered, player-visible observations."""

import copy

from civ_agent.protocol import JsonObject


def region_distance(a: JsonObject, b: JsonObject, wrap: JsonObject | None) -> int:
    def distance(offset: int):
        dx = a["x"] - b["x"] + offset * (wrap["x"] if wrap else 0)
        dy = a["y"] - b["y"] + offset * (wrap["y"] if wrap else 0)
        return max(abs(dx), abs(dy), abs(dx - dy))

    return min(distance(offset) for offset in (-1, 0, 1)) if wrap else distance(0)


def research_summary(snapshot: JsonObject) -> JsonObject | None:
    research = snapshot.get("research")
    if not research:
        return None
    # Always show the current choice, then the next queued choices and alternatives.
    queued = snapshot.get("researchQueue", [])
    names = list(
        dict.fromkeys(
            [
                *([research["current"]] if research.get("current") else []),
                *queued[:3],
                *snapshot.get("researchOptions", []),
            ]
        )
    )
    shown = names[:8]
    by_name = {tech["name"]: tech for tech in research.get("technologies", [])}
    return {
        "sciencePerTurn": research["sciencePerTurn"],
        "overflowScience": research["overflowScience"],
        "technologies": [copy.deepcopy(by_name[name]) for name in shown if name in by_name],
        "otherCandidateCount": max(0, len(names) - len(shown)),
        "estimateBasis": "Technology times use today's science rate and native stored/overflow science. They exclude waiting behind earlier research; unlocks can have other requirements. Full progress is in player_details(section=research).",
    }


def citizens_summary(city: JsonObject) -> JsonObject:
    development, management = city.get("development") or {}, city.get("management") or {}
    return {
        **{
            key: development[key]
            for key in ("focus", "avoidGrowth", "unassignedPopulation", "foodStored", "foodToNextPopulation")
            if key in development
        },
        **{key: management[key] for key in ("manualSpecialists",) if key in management},
        "specialists": [
            {key: s[key] for key in ("name", "assigned", "slots", "yields", "greatPersonPoints") if key in s}
            for s in management.get("specialists", [])
            if s.get("slots") or s.get("assigned")
        ],
        "greatPersonPointsPerTurn": {
            g["name"]: g["pointsPerTurn"] for g in management.get("greatPeople", []) if g.get("pointsPerTurn")
        },
    }


def resource_summary(economy: JsonObject) -> list[JsonObject]:
    return [
        {
            key: resource[key]
            for key in (
                "name",
                "type",
                "available",
                "used",
                "imported",
                "exported",
                "offered",
                "citiesDemanding",
                "citiesCelebrating",
            )
            if key in resource
        }
        for resource in economy.get("resources", [])
    ]


def exploration_summary(snapshot: JsonObject) -> JsonObject:
    view = snapshot.get("map", {})
    frontier, tiles, units = view.get("frontier", []), view.get("tiles", []), view.get("units", [])
    wrap = view.get("wrapOffset")

    def distance(a, b):
        return region_distance(a, b, wrap)

    approaches = [tile for tile in tiles if any(distance(tile["position"], unknown) == 1 for unknown in frontier)]
    points = {(p["x"], p["y"]): p for p in frontier}
    remaining = set(points)
    coverage = {
        id(tile): {key for key, point in points.items() if distance(tile["position"], point) <= 3}
        for tile in approaches
    }
    nearest = {
        id(tile): min((distance(tile["position"], u["position"]) for u in units), default=0) for tile in approaches
    }
    regions = []
    while remaining and approaches:
        # Pick explored approach tiles, never invent terrain or rewards in the fog.
        tile = min(
            approaches,
            key=lambda t: (-len(coverage[id(t)] & remaining), nearest[id(t)], t["position"]["x"], t["position"]["y"]),
        )
        covered = coverage[id(tile)] & remaining
        if not covered:
            break
        regions.append(
            {
                "approach": copy.deepcopy(tile["position"]),
                "terrain": tile.get("terrain"),
                "frontierTiles": len(covered),
                "nearbyUnits": [
                    {"unit": u["handle"], "distance": distance(tile["position"], u["position"])}
                    for u in sorted(units, key=lambda u: (distance(tile["position"], u["position"]), u["handle"]))[:2]
                ],
            }
        )
        remaining -= covered
        approaches.remove(tile)
    return {
        "regions": regions[:4],
        "otherRegionCount": max(0, len(regions) - 4),
        "limits": "Each explored approach borders unseen terrain. Counts group unexplored frontier coordinates within 3 hexes; no hidden terrain or resources are inspected. Nearby-unit distances are geographic, not routes or safety estimates. Inspect map_region and movement_preview, or use native exploration.",
    }


def happiness_buildings(snapshot: JsonObject) -> list[JsonObject]:
    """Use the existing native completion estimates; never sum independent scenarios."""
    wonders = {w["name"] for w in snapshot.get("wonderOpportunities", [])}
    result = []
    for city in snapshot.get("cities", []):
        options = []
        for name, estimate in city.get("constructionEstimates", {}).items():
            completion = estimate.get("completion") or {}
            change = completion.get("yields", {}).get("change", {}).get("Happiness", "0")
            if float(change) <= 0 or name in wonders:
                continue
            reasons = estimate.get("queueUnavailableReasons", [])
            if reasons:
                continue
            options.append(
                {
                    "name": name,
                    "happinessChange": change,
                    "maintenanceChange": completion["maintenanceChange"],
                    "turnsIfStartedNow": estimate["production"]["turnsIfStartedNow"],
                    "queued": name in city.get("constructionQueue", []),
                }
            )
        if options:
            result.append({"city": city["handle"], "options": sorted(options, key=lambda o: o["name"])})
    return result


def settlement_summary(settlement: JsonObject) -> JsonObject:
    result = {
        key: copy.deepcopy(value) for key, value in settlement.items() if key not in {"recommendations", "candidates"}
    }
    recommendations = settlement.get("recommendations", [])
    candidates = settlement.get("candidates", [])
    by_position = {(c["position"]["x"], c["position"]["y"]): c for c in candidates}
    selected = [by_position[p["x"], p["y"]] for p in recommendations] if recommendations else candidates[:3]
    result["candidates"] = copy.deepcopy(selected)
    budgets = []
    for candidate in result["candidates"]:
        budget = candidate.pop("happiness", None)
        if budget:
            result["happinessBasis"] = budget.pop("basis")
            group = next((group for group in budgets if group["budget"] == budget), None)
            if group is None:
                group = {"sites": [], "budget": budget}
                budgets.append(group)
            group["sites"].append(candidate["position"])
        else:
            candidate["happinessPreviewUnavailable"] = (
                "The city center has no recorded terrain observation for site-specific happiness estimates."
            )
        luxuries = {}
        for resource in candidate.get("resources", []):
            if resource["type"] == "Luxury" and resource.get("owner") is None:
                luxuries[resource["name"]] = resource.get("happinessIfConnected") or 0
        candidate["unclaimedLuxuryHappinessIfConnected"] = sum(luxuries.values())
        candidate["resources"] = expansion_resources(candidate.get("resources", []))
    result["newCityHappiness"] = budgets
    return result


def expansion_resources(resources: list[JsonObject]) -> JsonObject:
    """Share resource requirements while retaining every known tile and its age."""
    result: JsonObject = {"unclaimedPotential": [], "existingCityOverlap": [], "foreignOwned": []}
    for resource in resources:
        category = (
            "foreignOwned"
            if resource["ownership"] == "FOREIGN"
            else "existingCityOverlap"
            if resource["withinExistingCityRange"] or resource["ownership"] == "OWN"
            else "unclaimedPotential"
        )
        common = {
            key: resource[key]
            for key in ("name", "type", "currentlyAvailable", "happinessIfConnected", "builder", "improvements")
            if resource.get(key) is not None
        }
        group: JsonObject | None = next((g for g in result[category] if g["resource"] == common), None)
        if group is None:
            group = {"resource": common, "tiles": []}
            result[category].append(group)
        group["tiles"].append(
            {
                key: resource[key]
                for key in (
                    "position",
                    "distance",
                    "amount",
                    "owner",
                    "observedTurn",
                    "withinExistingCityRange",
                    "connectedByFounding",
                )
                if resource.get(key) is not None and resource[key] != []
            }
        )
    return {key: groups for key, groups in result.items() if groups}
