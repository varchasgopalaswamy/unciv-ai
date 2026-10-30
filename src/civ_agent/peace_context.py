"""Small, frozen peace evidence without construction offers or historical journals."""

import copy

from civ_agent.coordination_context import current_episode, encoded, relevant_messages
from civ_agent.protocol import JsonObject

MAX_PEACE_PROMPT_CHARACTERS = 64000

INSTRUCTIONS = (
    "This is a frozen peace brief. Exact terms and current accepted goals are preserved. "
    "Coverage counts describe supplied observations, never total foreign forces or cities. "
    "omitted counts identify facts not included; do not infer that omitted units, terrain, plans or messages "
    "do not exist. Remembered cities are dated observations, not confirmed current targets. "
    "Construction menus, executable action offers and historical journals are intentionally absent. "
    "State consequential unknowns and assumptions in your assessment."
)


def fields(value: JsonObject, names: tuple[str, ...]) -> JsonObject:
    return {key: copy.deepcopy(value[key]) for key in names if key in value}


def unit_brief(unit: JsonObject) -> JsonObject:
    result = fields(
        unit,
        (
            "handle",
            "name",
            "civilization",
            "military",
            "health",
            "position",
            "movement",
            "order",
            "embarked",
            "attacksRemaining",
            "strength",
            "rangedStrength",
        ),
    )
    if isinstance(unit.get("combat"), dict):
        result["combat"] = fields(
            unit["combat"],
            (
                "strength",
                "rangedStrength",
                "range",
                "order",
                "attacksRemaining",
                "fortificationTurns",
                "healingPerTurn",
            ),
        )
    return result


def city_brief(city: JsonObject) -> JsonObject:
    result = fields(
        city,
        (
            "handle",
            "name",
            "puppet",
            "health",
            "maxHealth",
            "defensiveStrength",
            "position",
            "population",
            "constructionQueue",
            "attacksRemaining",
        ),
    )
    if isinstance(city.get("development"), dict):
        result["development"] = fields(
            city["development"],
            ("position", "population", "yields", "foodPerTurn", "resistance", "raze", "turnsToGrowth"),
        )
    return result


def peace_prompt_inputs(inputs: JsonObject, role: str) -> JsonObject:
    """Project only caller-permitted facts, preserving exact protected records.

    Per-collection and overall budgets never cut strings or manufacture a
    summary of authored goals. An unusually large mandatory record is reported
    as an unavailable consultation before making a model request.
    """
    view, state = inputs["position"], inputs.get("warState") or {}
    position = fields(view, ("turn", "epoch", "revision", "phase", "speed", "difficulty", "eraProgress", "victory"))
    position["civilizationProfile"] = fields(view.get("civilizationProfile") or {}, ("name", "ability"))
    position["economy"] = fields(
        view.get("economy") or {},
        ("treasury", "committedGold", "availableGold", "goldPerTurn", "unitMaintenance", "happiness", "unitSupply"),
    )
    position["research"] = fields(view.get("research") or {}, ("current", "queue", "sciencePerTurn"))
    position["militaryReadiness"] = fields(
        view.get("militaryReadiness") or {}, ("roleCounts", "unitsExcludingRecon", "limits")
    )
    episodes = {
        key: current_episode(episode)
        for key, episode in state.get("episodes", {}).items()
        if episode.get("active", True)
    }
    # Pending proposals are discussion history, not accepted strategic goals.
    for episode in episodes.values():
        episode.pop("proposals", None)
    result: JsonObject = {
        "format": "peace-brief-v1",
        "position": position,
        "warState": {"episodes": episodes, "operations": [], "messages": []},
        "exactPeaceTerms": copy.deepcopy(inputs["exactPeaceTerms"]),
        "coverage": {"totals": {}, "omitted": {}, "instructions": INSTRUCTIONS},
    }
    if "militaryOverview" in inputs:
        result["militaryOverview"] = copy.deepcopy(inputs["militaryOverview"])
    limit = MAX_PEACE_PROMPT_CHARACTERS - 512
    if len(encoded(result)) > limit:
        raise ValueError("PEACE_REQUIRED_CONTEXT_TOO_LARGE")

    def collection(name, rows, budget, destination):
        admitted, used = [], 0
        result["coverage"]["totals"][name] = len(rows)
        result["coverage"]["omitted"][name] = len(rows)
        destination[name] = admitted
        for row in rows:
            cost = len(encoded(row))
            if used + cost > budget:
                continue
            admitted.append(copy.deepcopy(row))
            if len(encoded(result)) > limit:
                admitted.pop()
                continue
            used += cost
            result["coverage"]["omitted"][name] -= 1

    collection("cities", [city_brief(c) for c in view.get("cities", [])], 6000, position)
    units = view.get("units", (view.get("map") or {}).get("units", []))
    collection("units", [unit_brief(u) for u in units], 10000, position)
    tiles = (view.get("map") or {}).get("tiles", [])
    visible = [tile for tile in tiles if tile.get("visible")]
    city_fields = (
        "position",
        "owner",
        "cityName",
        "cityPopulation",
        "cityHealth",
        "cityMaxHealth",
        "cityDefensiveStrength",
        "cityIsOriginalMajorCapital",
        "visible",
        "observedTurn",
    )
    collection("visibleCities", [fields(t, city_fields) for t in visible if t.get("cityName")], 4000, position)
    collection(
        "rememberedCities",
        [fields(t, city_fields) for t in tiles if not t.get("visible") and t.get("cityName")],
        2500,
        position,
    )
    civilization = (view.get("civilizationProfile") or {}).get("name", view.get("civilization"))
    contacts = [
        {**unit_brief(u), "position": copy.deepcopy(t.get("position"))}
        for t in visible
        for u in t.get("units", [])
        if civilization is None or u.get("civilization") != civilization
    ]
    collection("visibleUnits", contacts, 6500, position)
    # Observed combat locations and city centers precede other visible terrain.
    visible = sorted(visible, key=lambda t: (not bool(t.get("units")), not bool(t.get("cityName"))))
    terrain_fields = (
        "position",
        "owner",
        "terrain",
        "features",
        "road",
        "roadPillaged",
        "riverBottom",
        "riverBottomLeft",
        "riverBottomRight",
    )
    collection("terrain", [fields(t, terrain_fields) for t in visible], 9000, position)
    diplomacy = view.get("diplomacy") or {}
    diplomacy_fields = (
        "civilizationId",
        "name",
        "atWar",
        "defeated",
        "status",
        "relationship",
        "relationshipLevel",
        "opinion",
        "peaceAvailable",
        "peaceUnavailableReason",
    )
    collection(
        "diplomacy",
        [fields(c, diplomacy_fields) for k in ("knownCivilizations", "cityStates") for c in diplomacy.get(k, [])],
        3500,
        position,
    )
    collection(
        "resources",
        [
            fields(r, ("name", "type", "amount", "available", "committed", "imported", "exported", "stockpiled"))
            for r in (view.get("economy") or {}).get("resources", [])
        ],
        2500,
        position,
    )
    collection(
        "operations",
        [
            {"id": key, **operation}
            for key, operation in state.get("operations", {}).items()
            if operation.get("status", "legacy") == "active"
        ],
        3500,
        result["warState"],
    )
    collection("messages", relevant_messages(state, role), 2500, result["warState"])
    memory = inputs.get("privateMemory")
    if memory:
        result["privateMemory"] = copy.deepcopy(memory)
        if len(encoded(memory)) > 13000 or len(encoded(result)) > limit:
            result.pop("privateMemory")
            result["coverage"]["omitted"]["privateMemory"] = 1
    result["coverage"]["omitted"] = {key: count for key, count in result["coverage"]["omitted"].items() if count}
    assert len(encoded(result)) <= MAX_PEACE_PROMPT_CHARACTERS
    return result
