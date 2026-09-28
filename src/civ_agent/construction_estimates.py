"""Select compact or detailed views of native city-specific construction estimates."""

import copy

from civ_agent.protocol import JsonObject

ESTIMATE_BASIS = (
    "Completion compares ongoing city yields at current population and citizen assignments, with no "
    "production conversion or construction-specific bonus on either side. Food is net surplus; Gold "
    "includes maintenance. Growth shows native rounded food accumulation and growth/starvation times "
    "if completed now. Specialist slots are unstaffed opportunities. Completion triggers such as "
    "free buildings, units, technologies and population, future growth, and yields in other cities are "
    "excluded; read the building's effects too. While-building changes compare starting this item with "
    "the current construction. Turns assume starting now, using stored production and overflow, and "
    "exclude time behind earlier queue entries. Blocked choices remain unavailable. Null construction time means "
    "no current completion estimate. Omitted yield changes are zero at displayed precision."
    " Named victory projects and spaceship parts include costs and requirements even before their technology "
    "unlocks; their hypothetical build times exclude time to satisfy those prerequisites."
    " Queue progress supplies production for individual queued copies. In named construction reads, "
    "a progress row includes production only when it differs from the entry's production estimate."
)


def queue_progress(progress: JsonObject, shared_production: JsonObject | None = None) -> JsonObject:
    fields = {
        "productionCost": "cost",
        "productionStored": "stored",
        "productionPerTurn": "perTurn",
        "turns": "turnsIfStartedNow",
    }
    result = {key: copy.deepcopy(value) for key, value in progress.items() if key not in fields}
    production = {public: progress[native] for native, public in fields.items() if native in progress}
    if production and (shared_production is None or any(shared_production.get(k) != v for k, v in production.items())):
        result["production"] = production
    return result


def city_construction_data(city: JsonObject) -> None:
    """Queue progress owns per-copy production; estimates retain the benefits."""
    progress = (city.get("development") or {}).get("constructions")
    if progress is None:
        return
    city["development"]["constructions"] = [queue_progress(row) for row in progress]
    for row in progress:
        if row["name"] in city.get("constructionEstimates", {}):
            city["constructionEstimates"][row["name"]].pop("production", None)


def completion_summary(completion: JsonObject) -> JsonObject:
    result = {
        "yieldChange": {stat: value for stat, value in completion["yields"]["change"].items() if float(value) != 0},
        "maintenanceChange": completion["maintenanceChange"],
        **{
            key: copy.deepcopy(completion[key])
            for key in ("specialistSlots", "greatPersonPoints")
            if completion.get(key)
        },
    }
    growth = completion.get("growth")
    if growth and (
        completion["yields"]["change"].get("Food", "0") != "0"
        or any(
            growth[key + "Before"] != growth[key + "After"]
            for key in ("foodPerTurn", "turnsToGrowth", "turnsToStarvation")
        )
    ):
        result["growth"] = copy.deepcopy(growth)
    return result


def construction_estimate(estimate: JsonObject, *, detailed: bool = False) -> JsonObject:
    if detailed:
        return copy.deepcopy(estimate)
    result = {"production": copy.deepcopy(estimate["production"])}
    if estimate.get("queueUnavailableReasons"):
        result["queueUnavailableReasons"] = copy.deepcopy(estimate["queueUnavailableReasons"])
    if estimate.get("completion") is not None:
        result["completion"] = completion_summary(estimate["completion"])
    change = estimate["whileBuilding"]["change"]
    if change:
        result["whileBuildingChange"] = copy.deepcopy(change)
    return result


def overview_estimates(city: JsonObject, wonders: set[str]) -> JsonObject:
    names = city.get("constructionQueue", [])[:1] + (city.get("constructionChoice") or {}).get(
        "availableSinceChoice", []
    )
    estimates = city.get("constructionEstimates", {})
    return {name: construction_estimate(estimates[name]) for name in sorted(set(names) - wonders) if name in estimates}
