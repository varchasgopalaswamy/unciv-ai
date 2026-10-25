"""Select compact or detailed views of native city-specific construction estimates."""

import copy
import math

from civ_agent.protocol import JsonObject

INVESTMENT_BASIS = (
    "Compare today's output with starting this construction now, keeping rates, population and citizen "
    "assignments fixed. Benefits begin after the estimated build time. This is a comparison, not a forecast "
    "or recommendation: it leaves out growth, one-time rewards, later builds, new specialists and rival responses. "
    "Effects on the empire are shown separately as if completed now. Gold includes maintenance; purchasing "
    "costs extra and completes immediately. The main comparison uses no production conversion. Following "
    "scenarios compare converting now with building or buying first, then choosing that conversion. "
    "These scenarios leave the actual queue unchanged; adding, moving and continuing an item are separate choices. "
    "Break-even means recovering yields lost while building, once later yields improve; it does not repay "
    "the production or purchase cost. Happiness is a level, not an accumulating yield. Only shared stats are compared."
)


def investment_comparison(
    estimate: JsonObject,
    purchase: JsonObject | None,
    horizon: int,
    *,
    progress: list[JsonObject] | None = None,
    can_queue: bool | None = None,
) -> JsonObject:
    """Arithmetic over native current-state previews, not a future city simulation."""
    result: JsonObject = {
        "horizonTurns": horizon,
        "basis": INVESTMENT_BASIS,
    }
    completion = estimate.get("completion")
    if completion is None:
        return {**result, "available": False, "reason": "No native building-completion preview"}
    progress = progress or []
    build_reasons = estimate.get("buildUnavailableReasons", estimate.get("queueUnavailableReasons", []))
    current = next((p for p in progress if p["index"] == 0), None)
    move = next((p for p in progress if 0 in p.get("moveToIndices", [])), None)
    append = can_queue if can_queue is not None else not estimate.get("queueUnavailableReasons")
    operation = (
        {"kind": "CONTINUE", "index": 0}
        if current
        else {"kind": "MOVE_CONSTRUCTION", "index": move["index"], "toIndex": 0}
        if move
        else {"kind": "QUEUE_CONSTRUCTION", "thenMoveToFront": True}
        if append
        else None
    )
    result["queueOptions"] = {
        "canAppend": append,
        "buildNow": operation,
        "appendUnavailableReasons": estimate.get("queueUnavailableReasons", []),
    }
    before = estimate.get("whileBuilding", {}).get("before", {})
    during = estimate.get("whileBuilding", {}).get("after", {})
    after = completion["yields"]["after"]
    stats = sorted((before.keys() & during.keys() & after.keys()) - {"Happiness"})
    if not stats:
        return {**result, "available": False, "reason": "No comparable native yield rates"}
    baseline = {s: float(before[s]) for s in stats}
    gain = {s: float(after[s]) - baseline[s] for s in stats}
    result.update(available=True, continueCurrent={s: round(baseline[s] * horizon, 2) for s in stats})
    if "Happiness" in before and "Happiness" in after:
        result["happiness"] = {
            "current": float(before["Happiness"]),
            "afterCompletion": float(after["Happiness"]),
            "difference": round(float(after["Happiness"]) - float(before["Happiness"]), 2),
        }
    turns = estimate.get("production", {}).get("turnsIfStartedNow")
    if build_reasons or operation is None or turns is None:
        result["build"] = {
            "available": False,
            "reasons": build_reasons
            or estimate.get("queueUnavailableReasons")
            or ["No current ETA or build operation"],
        }
    else:
        delay = max(0, turns)
        construction_delta = {s: float(during[s]) - baseline[s] for s in stats}
        result["build"] = {
            "available": True,
            "completionInTurns": turns,
            "yieldDifferenceOverHorizon": {
                s: round(construction_delta[s] * min(horizon, delay) + gain[s] * max(0, horizon - delay), 2)
                for s in stats
            },
            "postCompletionRateDifference": {s: round(gain[s], 2) for s in stats},
            "breakEvenTurnsByStat": {
                s: delay + math.ceil(max(0, -construction_delta[s] * delay) / gain[s]) if gain[s] > 0 else None
                for s in stats
            },
            "endOfHorizon": {
                "completed": horizon >= delay,
                "turnsRemainingAtCurrentRate": max(0, delay - horizon),
                "storedProduction": 0
                if horizon >= delay
                else min(
                    estimate["production"].get("cost", 0),
                    estimate["production"].get("stored", 0)
                    + estimate["production"].get("overflow", 0)
                    + estimate["production"].get("perTurn", 0) * horizon,
                ),
                "postCompletionYieldsPerTurn": copy.deepcopy(after),
                "asset": "Completed building" if horizon >= delay else "Construction in progress",
            },
        }
    result["purchase"] = (
        {
            "available": True,
            "goldCostNow": purchase["goldCost"],
            "yieldDifferenceOverHorizon": {s: round(gain[s] * horizon, 2) for s in stats},
            "endOfHorizon": {
                "completed": True,
                "asset": "Completed building",
                "postCompletionYieldsPerTurn": copy.deepcopy(after),
            },
        }
        if purchase and purchase.get("available") and purchase.get("goldCost") is not None
        else {"available": False}
    )
    if completion.get("empire"):
        result["empireEffectIfCompletedNow"] = copy.deepcopy(completion["empire"])
    if completion.get("following"):
        result["following"] = {}
        for name, yields in completion["following"].items():
            scenario = copy.deepcopy(estimate)
            scenario["whileBuilding"]["before"] = yields["before"]
            scenario["completion"]["yields"] = yields
            scenario["completion"].pop("following", None)
            compared = investment_comparison(scenario, purchase, horizon, progress=progress, can_queue=can_queue)
            result["following"][name] = {
                key: compared[key] for key in ("available", "continueCurrent", "build", "purchase")
            }
            result["following"][name]["baseline"] = "Select " + name + " now and continue at its current native rate"
    return result


ESTIMATE_BASIS = (
    "Estimates keep current population and citizen assignments fixed. Completion compares normal city yields "
    "before and after building, without production conversion or construction-specific bonuses. Food is surplus "
    "after consumption; Gold includes maintenance. Growth shows rounded food accumulation and time to growth "
    "or starvation if completed now. Specialist slots need citizens to fill them. One-time rewards and empire "
    "happiness and food effects are shown separately, not added to ongoing city yields. Future growth and new "
    "citizen assignments are excluded. Following scenarios show choosing a production conversion after completion, "
    "including building bonuses; the real queue stays unchanged. While-building changes compare starting this "
    "item with the current construction. Build times assume starting now, with the stated work and overflow. "
    "They exclude waiting behind other items or meeting missing prerequisites, including for locked victory "
    "projects and spaceship parts. Unavailable choices remain unavailable; a missing time means no estimate. "
    "Unlisted yield changes round to zero. A queued copy's progress is shown separately when it differs from "
    "the general estimate. Appending adds another copy without reusing work already invested in a queued copy. "
    "Its estimate uses current overflow and excludes queue delay."
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
            for key in ("specialistSlots", "greatPersonPoints", "oneTime")
            if completion.get(key)
        },
    }
    if empire := completion.get("empire"):
        result["empireHappinessChange"] = empire["happinessAfter"] - empire["happinessBefore"]
        result["empireFoodChange"] = {
            city: row["foodPerTurnAfter"] - row["foodPerTurnBefore"]
            for city, row in empire["foodPerTurnByCity"].items()
            if row["foodPerTurnAfter"] != row["foodPerTurnBefore"]
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
