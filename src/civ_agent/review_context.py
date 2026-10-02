"""Small factual comparisons and timelines from the review's permitted position."""

import copy
from collections import Counter

from civ_agent.protocol import JsonObject

TIMELINE_TURNS = 10
MAX_EVENTS = 16
MAX_CHANGES = 16


def number(value):
    if value is None:
        return None
    value = float(value)
    return int(value) if value.is_integer() else value


def review_position(snapshot: JsonObject) -> JsonObject:
    """Retain only owned facts needed at the next review, without plans or prose."""
    cities = {
        city["handle"]: {
            "name": city["name"],
            "population": (city.get("development") or {}).get("population"),
            "buildings": (city.get("development") or {}).get("builtBuildings", []),
        }
        for city in snapshot.get("cities", [])
    }
    production = [
        next(
            (
                number(y["amount"])
                for y in (city.get("development") or {}).get("yields", [])
                if y["stat"] == "Production"
            ),
            None,
        )
        for city in snapshot.get("cities", [])
    ]
    population = [city["population"] for city in cities.values()]
    military = {
        unit["handle"]: unit["name"] for unit in snapshot.get("map", {}).get("units", []) if unit.get("military")
    }
    economy = snapshot.get("economy") or {}
    research = snapshot.get("research") or {}
    return copy.deepcopy(
        {
            "turn": snapshot["turn"],
            "totals": {
                "cities": len(cities),
                "population": sum(n for n in population if n is not None)
                if all(n is not None for n in population)
                else None,
                "sciencePerTurn": number(research.get("sciencePerTurn")),
                "culturePerTurn": number((snapshot.get("culture") or {}).get("culturePerTurn")),
                "productionPerTurn": sum(n for n in production if n is not None)
                if all(n is not None for n in production)
                else None,
                "netGoldPerTurn": economy.get("goldPerTurn"),
                "happiness": economy.get("happiness"),
                "militaryUnits": len(military),
            },
            "cities": cities,
            "military": military,
            "technologies": sorted(t["name"] for t in research.get("technologies", []) if t["researched"]),
        }
    )


def development_changes(before: JsonObject | None, after: JsonObject) -> JsonObject:
    result: JsonObject = {"fromTurn": before["turn"] if before else None, "toTurn": after["turn"]}
    if before is None:
        result["available"] = False
        return result
    if before["turn"] >= after["turn"]:
        raise ValueError("STRATEGIC_REVIEW_BASELINE_NOT_EARLIER")
    result["available"] = True
    result["totals"] = {
        key: {"before": before["totals"][key], "now": value, "change": round(value - before["totals"][key], 2)}
        for key, value in after["totals"].items()
        if value is not None and before["totals"][key] is not None
    }
    result["basis"] = (
        "Endpoint observations, not interval averages. Production sums displayed city yields with current construction modifiers. "
        "Ownership/building additions can include purchases, gifts or captures; removals do not establish destruction. "
        "Recent accepted actions provide additional context."
    )

    def changes(key, rows):
        if rows:
            result[key] = rows[:MAX_CHANGES]
            if len(rows) > MAX_CHANGES:
                result.setdefault("omittedChanges", {})[key] = len(rows) - MAX_CHANGES

    old_cities, cities = before["cities"], after["cities"]
    for key, source, other in (("citiesAdded", cities, old_cities), ("citiesNoLongerOwned", old_cities, cities)):
        changes(
            key, [{"city": handle, "name": source[handle]["name"]} for handle in sorted(source.keys() - other.keys())]
        )
    buildings = []
    for handle in sorted(cities.keys() & old_cities.keys()):
        added = sorted(set(cities[handle]["buildings"]) - set(old_cities[handle]["buildings"]))
        if added:
            row: JsonObject = {"city": handle, "name": cities[handle]["name"], "buildings": added[:MAX_CHANGES]}
            if len(added) > MAX_CHANGES:
                row["omittedBuildings"] = len(added) - MAX_CHANGES
            buildings.append(row)
    changes("buildingsAdded", buildings)
    changes("technologiesAcquired", sorted(set(after["technologies"]) - set(before["technologies"])))
    old_units, units = before["military"], after["military"]
    for key, source, other in (("militaryAdded", units, old_units), ("militaryNoLongerOwned", old_units, units)):
        counts = Counter(source[handle] for handle in source.keys() - other.keys())
        changes(key, [{"name": name, "count": count} for name, count in sorted(counts.items())])
    changes(
        "militaryTypeChanges",
        [
            {"unit": handle, "before": old_units[handle], "now": units[handle]}
            for handle in sorted(units.keys() & old_units.keys())
            if units[handle] != old_units[handle]
        ],
    )
    return result


def upcoming_events(snapshot: JsonObject, social: JsonObject) -> JsonObject:
    events = []

    def add(kind, turns, **facts):
        # Native research uses text for "infinity"; absent/negative ETAs are not dates.
        if isinstance(turns, str):
            turns = int(turns) if turns.isdecimal() else None
        if type(turns) is int and 0 <= turns <= TIMELINE_TURNS:
            events.append({"kind": kind, "inTurns": turns, **facts})

    research = snapshot.get("research") or {}
    current = next((t for t in research.get("technologies", []) if t["name"] == research.get("current")), None)
    if current and not current["researched"]:
        add(
            "RESEARCH_COMPLETES",
            current["turns"],
            name=current["name"],
            unlocks={key: value for key, value in (current.get("unlocks") or {}).items() if value},
        )
    wonders = {w["name"] for w in snapshot.get("wonderOpportunities", [])}
    for city in snapshot.get("cities", []):
        development = city.get("development") or {}
        if development.get("growing") and not any(
            development.get(key) for key in ("beingRazed", "avoidGrowth", "inResistance")
        ):
            add(
                "CITY_GROWTH",
                development.get("turnsToGrowth"),
                city=city["handle"],
                name=city["name"],
                populationAfterGrowth=development["population"] + 1,
            )
        if development.get("starving"):
            add("CITY_STARVATION", development.get("turnsToStarvation"), city=city["handle"], name=city["name"])
        # Later queue entries expose 'if started now' estimates, not completion dates.
        active = next((c for c in development.get("constructions", []) if c["index"] == 0), None)
        if active and not active["perpetual"] and active["name"] not in wonders and not development.get("inResistance"):
            estimate = city.get("constructionEstimates", {}).get(active["name"], {})
            if not estimate.get("queueUnavailableReasons"):
                completion = estimate.get("completion") or {}
                benefits = {
                    key: value
                    for key, value in completion.get("yields", {}).get("change", {}).items()
                    if number(value) != 0
                }
                add(
                    "CONSTRUCTION_COMPLETES",
                    active.get("turns"),
                    city=city["handle"],
                    name=active["name"],
                    yieldChange=benefits,
                    maintenanceChange=completion.get("maintenanceChange"),
                )

    # Each native trade term has its own remaining duration; the agreement maximum
    # can hide an earlier resource expiry. One-time gold/city transfers are omitted.
    peace_partners = set()
    for agreement in social.get("agreements", []):
        if agreement.get("status") != "ACTIVE":
            continue
        durations: dict[int, JsonObject] = {}
        for direction in ("give", "receive"):
            for term in agreement.get(direction, []):
                duration = term.get("duration", 0)
                if duration <= 0:
                    continue
                if term.get("name") == "Peace Treaty":
                    peace_partners.add(agreement["partner"])
                group = durations.setdefault(duration, {"give": [], "receive": []})
                group[direction].append({key: term[key] for key in ("type", "name", "amount") if key in term})
        for duration, terms in sorted(durations.items()):
            add(
                "AGREEMENT_TERMS_END",
                duration,
                agreement=agreement["agreement"],
                partner=agreement["partner"],
                **{key: rows for key, rows in terms.items() if rows},
            )

    diplomacy = snapshot.get("diplomacy") or {}
    for relation in diplomacy.get("knownCivilizations", []):
        turns = relation.get("peaceTreatyTurns", 0)
        if turns > 0 and relation["civilizationName"] not in peace_partners:
            add("PEACE_TREATY_ENDS", turns, partner=relation["civilizationName"])
    for state in diplomacy.get("cityStates", []):
        if (
            not state.get("atWar")
            and not state.get("defeated")
            and state.get("relationship") in {"Friend", "Ally"}
            and state.get("turnsToRelationshipChange", 0) > 0
        ):
            add(
                "CITY_STATE_RELATIONSHIP_CHANGE",
                state.get("turnsToRelationshipChange"),
                name=state["name"],
                relationship=state["relationship"],
                influence=state["influence"],
                decayPerTurn=state["decayPerTurn"],
            )

    events.sort(
        key=lambda event: (
            event["inTurns"],
            event["kind"],
            event.get("city", ""),
            event.get("partner", ""),
            event.get("name", ""),
        )
    )
    return copy.deepcopy(
        {
            "asOfTurn": snapshot["turn"],
            "horizonTurns": TIMELINE_TURNS,
            "happinessNow": (snapshot.get("economy") or {}).get("happiness"),
            "basis": (
                "Current research, active non-wonder construction and first city growth/starvation only; later queue entries are not scheduled here. "
                "ETAs use current rates. Unlocks can require resources or other prerequisites. Building yield changes compare the current city; "
                "they are not additive forecasts of future happiness. Growth can change happiness; use the current happiness budget. "
                "Trade terms use native remaining durations; city-state estimates assume current influence decay without other players' intervention. "
                "Wonder estimates remain under wonderOpportunities."
            ),
            "events": events[:MAX_EVENTS],
            "omittedEvents": max(0, len(events) - MAX_EVENTS),
        }
    )
