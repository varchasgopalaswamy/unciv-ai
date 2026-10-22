"""Bounded city choices over frozen, permitted native data; never execute advice."""

import asyncio
import copy
import re

import aiohttp

from civ_agent.construction_estimates import ESTIMATE_BASIS, construction_estimate
from civ_agent.decisions_ledger import fingerprint
from civ_agent.decisions_tasks import CITY_LIMIT, CONSTRUCTION_LIMIT, FOCUS_LIMIT
from civ_agent.observations import DetailQuery, project_detail
from civ_agent.protocol import JsonObject

LIMITATIONS = [
    "Each suggestion assumes the current city, without applying the other suggestion. The player decides how to balance cities.",
    "Check the selected option's details before acting. Keeping leaves the queue alone; adding and moving are separate choices.",
    "Adding means another item. It can replace an idle order or go before a final production conversion.",
    "Moving an item to the front can change its credited work: the first queued copy of a construction gets the stored work.",
    "A focus guides future citizens too; matching yields today do not make two focuses equivalent.",
]


def adopted_decisions(objectives):
    return [
        {
            "source": {k: source[k] for k in ("key", "version")},
            **{
                k: copy.deepcopy(decision[k])
                for k in ("id", "objective", "investment", "reconsiderWhen", "subjects")
                if k in decision
            },
        }
        for source in objectives
        for decision in source.get("record", {}).get("decisions", [])
    ]


def construction_options(city, production, details):
    """Bind one legal operation and its own native work to each alternative."""
    estimates = city.get("constructionEstimates", {})
    rows: list[JsonObject] = []
    by_name = {option["name"]: option for option in details}
    for progress in production.get("data", {}).get("entity", {}).get("development", {}).get("constructions", []):
        command = next((c for c in progress.get("moveCommands", []) if c["toIndex"] == 0), None)
        if command is None or progress["name"] not in by_name:
            continue
        estimate = estimates.get(progress["name"])
        if estimate is None:
            continue
        native = construction_estimate(estimate)
        # Native work is stored by construction name. Moving a later copy
        # ahead of its siblings makes it the first copy and gives it that work.
        native["production"] = copy.deepcopy(estimate["production"])
        rows.append(
            {
                "id": f"move:{progress['index']}",
                "native": {
                    **native,
                    "name": progress["name"],
                    "command": copy.deepcopy(command),
                    "queueOperation": "move_to_front",
                    "queueIndex": progress["index"],
                    "previousQueueProduction": copy.deepcopy(progress["production"]),
                    "queueDelayTurns": 0,
                    **{
                        k: copy.deepcopy(estimate[k])
                        for k in ("requiredTechnologies", "resourceRequirements")
                        if k in estimate
                    },
                },
            }
        )
    for name, option in by_name.items():
        command = option.get("command")
        estimate = estimates.get(name)
        if command is None or estimate is None:
            continue
        native = construction_estimate(estimate)
        native["production"] = copy.deepcopy(estimate["appendProduction"])
        rows.append(
            {
                "id": f"append:{name}",
                "native": {
                    **native,
                    "name": name,
                    "command": copy.deepcopy(command),
                    "queueOperation": "append",
                    "existingQueue": copy.deepcopy(city.get("constructionQueue", [])),
                    "additionalCopy": name in city.get("constructionQueue", []),
                    "queueDelayTurns": None if city.get("constructionQueue") else 0,
                    "completionAfterQueueTurns": None,
                    **{
                        k: copy.deepcopy(estimate[k])
                        for k in ("requiredTechnologies", "resourceRequirements")
                        if k in estimate
                    },
                },
            }
        )
    name_order = {name: index for index, name in enumerate(by_name)}
    rows.sort(key=lambda row: (name_order[row["native"]["name"]], row["native"]["queueOperation"] == "append"))
    return rows[:CONSTRUCTION_LIMIT], len(rows)


def basis(gateway, job):
    observation = job.observation or {}
    return {
        "player": gateway.memory.civilization,
        "epoch": observation.get("epoch"),
        "revision": observation.get("revision"),
        "turn": observation.get("turn"),
    }


def current(gateway, job, captured):
    return captured == basis(gateway, job)


def change_key(job, objectives):
    """Observe decision changes without treating routine stored work as news."""
    observation = job.observation or {}
    return fingerprint(
        {
            "objectives": objectives,
            "reviewAdvice": job.memory.get("strategicAdvice"),
            "cities": [
                {
                    **{k: city.get(k) for k in ("handle", "puppet", "constructionQueue", "constructionOptions")},
                    "constructionChoice": {
                        k: (city.get("constructionChoice") or {}).get(k)
                        for k in ("name", "chosenTurn", "availableSinceChoice")
                    },
                    "development": {
                        k: city.get("development", {}).get(k)
                        for k in (
                            "population",
                            "focus",
                            "avoidGrowth",
                            "foodPerTurn",
                            "starving",
                            "inResistance",
                            "tiles",
                        )
                    },
                    "management": {
                        "manualSpecialists": (city.get("management") or {}).get("manualSpecialists"),
                        "focusOptions": [
                            {"name": option.get("name"), "available": option.get("action", {}).get("available")}
                            for option in (city.get("management") or {}).get("focusOptions", [])
                        ],
                    },
                }
                for city in observation.get("cities", [])
            ],
            "economy": {
                k: observation.get("economy", {}).get(k)
                for k in ("happiness", "goldPerTurn", "unitMaintenance", "resources")
            },
            "units": [
                {k: unit.get(k) for k in ("handle", "name", "military")}
                for unit in observation.get("map", {}).get("units", [])
            ],
            "visibleHostiles": (observation.get("tacticalReview") or {}).get("visibleHostiles", []),
        }
    )


async def internal_read(gateway, job, name, arguments):
    """Account for host work without registering an inspected/delivered source."""
    counted = (
        await gateway.io.call(gateway.accounting.begin_call, job.id, name, arguments, source="internal")
        if gateway.accounting
        else None
    )
    outcome = "error"
    try:
        async with asyncio.timeout(5):
            value = await job.connection.tool(name, arguments)
        gateway.check(job)
        outcome = "error" if not isinstance(value, dict) or "error" in value else "success"
        return value
    finally:
        if counted:
            await gateway.io.call(gateway.accounting.end_call, counted, outcome, False)


async def capture(gateway, job, objectives) -> tuple[list[JsonObject], JsonObject, JsonObject]:
    observation = job.observation or {}
    if job.purpose not in {"gameplay", "economy"} or observation.get("phase") != "ACTIVE":
        return [], {}, {"reason": "not_active_gameplay", "capturedCities": 0}
    captured_basis = basis(gateway, job)
    candidates = [
        c
        for c in observation.get("cities", [])
        if not c.get("puppet")
        and any(o.get("action", {}).get("available") for o in (c.get("management") or {}).get("focusOptions", []))
    ]
    candidates.sort(key=lambda c: (bool(c.get("constructionQueue")), c["handle"]))
    selected = candidates[:CITY_LIMIT]
    context: JsonObject = {
        "economy": {
            k: copy.deepcopy(v)
            for k, v in observation.get("economy", {}).items()
            if k in {"happiness", "treasury", "goldPerTurn", "unitMaintenance"}
        },
        "cities": [
            {k: c.get(k) for k in ("handle", "name", "constructionQueue")} for c in observation.get("cities", [])
        ],
        "researchQueue": copy.deepcopy(observation.get("researchQueue", [])),
        "visibleHostiles": copy.deepcopy((observation.get("tacticalReview") or {}).get("visibleHostiles", [])),
        "reviewAdvice": copy.deepcopy(job.memory.get("strategicAdvice")),
        "adoptedDecisions": adopted_decisions(objectives),
    }
    context["economy"]["resources"] = [
        {k: r.get(k) for k in ("name", "available", "used", "imported", "exported")}
        for r in observation.get("economy", {}).get("resources", [])
    ]
    # Raw snapshots carry map.units; only an owned-unit roster is needed here.
    context["units"] = [
        {k: u.get(k) for k in ("handle", "name", "military")} for u in observation.get("map", {}).get("units", [])
    ]
    words = set(re.findall(r"\w{3,}", str([objectives, context["reviewAdvice"]]).casefold()))
    cities = []
    failures = 0
    for city in selected:
        try:
            preview = await internal_read(
                gateway, job, "player_inspect", {"subject": "CITY_FOCUS", "target": city["handle"]}
            )
        except (TimeoutError, OSError, ValueError, aiohttp.ClientError):
            failures += 1
            continue
        if not isinstance(preview, dict):
            failures += 1
            continue
        if any(preview.get(k) != captured_basis[k] for k in ("epoch", "revision", "turn")) or not current(
            gateway, job, captured_basis
        ):
            return [], {}, {"reason": "stale_native_input", "capturedCities": len(cities)}
        data = preview.get("data", {})
        if not isinstance(data, dict):
            failures += 1
            continue
        focuses = data.get("options", [])
        if (
            not data.get("available")
            or data.get("city") != city["handle"]
            or not isinstance(focuses, list)
            or not focuses
        ):
            failures += 1
            continue
        legal = {
            o["name"]
            for o in (city.get("management") or {}).get("focusOptions", [])
            if o.get("action", {}).get("available")
        }
        focuses = [
            o
            for o in focuses
            if isinstance(o, dict)
            and o.get("name") in legal
            and o.get("command") == {"kind": "SET_CITY_FOCUS", "target": city["handle"], "selection": o.get("name")}
        ]
        if not focuses:
            failures += 1
            continue
        production = project_detail(
            observation,
            DetailQuery(tool="city_details", arguments={"target": city["handle"], "sections": ["production"]}),
        )
        move_names = {
            p["name"]
            for p in production.get("data", {}).get("entity", {}).get("development", {}).get("constructions", [])
            if any(command["toIndex"] == 0 for command in p.get("moveCommands", []))
        }
        all_names = set(city.get("constructionOptions", [])) | move_names
        names = list(all_names)
        available_since = (city.get("constructionChoice") or {}).get("availableSinceChoice", [])
        names.sort(key=lambda n: (n not in available_since, -len(words & set(re.findall(r"\w{3,}", n.casefold()))), n))
        names = names[:CONSTRUCTION_LIMIT]
        constructions, total_constructions = [], 0
        if names:
            details = project_detail(
                observation,
                DetailQuery(tool="construction_details", arguments={"target": city["handle"], "names": names}),
            )
            constructions, total_constructions = construction_options(
                city, production, details.get("data", {}).get("constructions", [])
            )
        development = city.get("development", {})
        facts = {
            k: copy.deepcopy(development[k])
            for k in (
                "population",
                "focus",
                "avoidGrowth",
                "foodPerTurn",
                "turnsToGrowth",
                "turnsToStarvation",
                "yields",
                "tiles",
            )
            if k in development
        }
        facts.update(
            queue=copy.deepcopy(city.get("constructionQueue", [])),
            manualSpecialists=(city.get("management") or {}).get("manualSpecialists"),
            adoptedDecisions=[
                decision
                for decision in context["adoptedDecisions"]
                if {city["handle"], city["name"]} & set(decision.get("subjects", []))
            ],
        )
        head = next(iter(city.get("constructionQueue", [])), None)
        if head in city.get("constructionEstimates", {}):
            facts["currentConstructionEstimate"] = construction_estimate(city["constructionEstimates"][head])
        cities.append(
            {
                "city": city["handle"],
                "name": city["name"],
                "basis": copy.deepcopy(captured_basis),
                "facts": facts,
                "focuses": [
                    {"id": f"focus{i}", "native": copy.deepcopy(o)} for i, o in enumerate(focuses[:FOCUS_LIMIT])
                ],
                "constructions": constructions,
                "omittedFocuses": max(0, len(legal) - min(len(focuses), FOCUS_LIMIT)),
                "omittedConstructions": max(0, len(all_names) - len(names))
                + max(0, total_constructions - len(constructions)),
                "limitations": [*data.get("limitations", []), ESTIMATE_BASIS, *LIMITATIONS],
            }
        )
    rule_names = sorted({o["native"]["name"] for c in cities for o in c["constructions"]})
    context["constructionRules"] = []
    for offset in range(0, len(rule_names), 16):
        try:
            reference = await internal_read(
                gateway, job, "player_inspect", {"subject": "CIVILOPEDIA", "names": rule_names[offset : offset + 16]}
            )
        except (TimeoutError, OSError, ValueError, aiohttp.ClientError):
            continue
        if not isinstance(reference, dict) or "error" in reference:
            continue
        if any(reference.get(k) != captured_basis[k] for k in ("epoch", "revision", "turn")):
            return [], {}, {"reason": "stale_native_input", "capturedCities": len(cities)}
        for item in reference.get("data", {}).get("items", []):
            if isinstance(item, dict) and item.get("name") in rule_names:
                context["constructionRules"].append(copy.deepcopy(item))
    context["omittedRuleNames"] = sorted(set(rule_names) - {r["name"] for r in context["constructionRules"]})
    return (
        cities,
        context,
        {
            "capturedCities": len(cities),
            "omittedCities": max(0, len(candidates) - len(cities)),
            "unavailableCities": failures,
        },
    )


def semantic_key(cities, context, objectives):
    """Transport changes and routine countdowns must not cause paid reclassification."""

    def normalize(value):
        if isinstance(value, list):
            return [normalize(v) for v in value]
        if not isinstance(value, dict):
            return value
        result = {}
        for key, item in value.items():
            if key in {"basis", "treasury", "stockpiles", "stored", "overflow", "foodStored", "productionStored"}:
                continue
            if key in {"constructionTurns", "turnsIfStartedNow", "turnsToGrowth", "turnsToStarvation"}:
                item = (
                    next((limit for limit in (1, 3, 10, 30, 100) if item <= limit), 101)
                    if isinstance(item, (int, float))
                    else item
                )
            result[key] = normalize(item)
        return result

    return fingerprint(normalize({"cities": cities, "context": context, "objectives": objectives}))


def recommendations(gateway, job, cities, result, context=None):
    answers = {a["name"]: a for a in result.get("answers", [])}
    rows = []
    for index, city in enumerate(cities):
        if not current(gateway, job, city["basis"]):
            continue
        row = {
            k: copy.deepcopy(city[k])
            for k in ("city", "name", "basis", "omittedFocuses", "omittedConstructions", "limitations")
        }
        for field, prefix, candidates in (
            ("focus", "city_focus", "focuses"),
            ("construction", "city_construction", "constructions"),
        ):
            change = answers.get(f"{prefix}_change:{index}")
            if not change:
                continue
            if change["choice"] == "keep":
                row[field] = {
                    "source": "decisions",
                    "choice": change["choice"],
                    "confidence": change["confidence"],
                    "change": copy.deepcopy(change),
                }
                continue
            if change["choice"] != "change":
                continue
            answer = answers.get(f"{prefix}:{index}")
            if not answer:
                continue
            selected = answer["choice"]
            option = next((o for o in city[candidates] if o["id"] == selected), None)
            if option is None and selected != "defer":
                continue
            row[field] = {
                "source": "decisions",
                "choice": selected,
                "confidence": min(change["confidence"], answer["confidence"]),
                "change": copy.deepcopy(change),
            }
            if option:
                row[field]["native"] = copy.deepcopy(option["native"])
                if field == "construction":
                    row[field]["native"]["rules"] = [
                        r for r in (context or {}).get("constructionRules", []) if r["name"] == option["native"]["name"]
                    ]
                row[field]["inspection"] = (
                    {"tool": "city_details", "arguments": {"target": city["city"], "sections": ["citizens"]}}
                    if field == "focus"
                    else {
                        "tool": "construction_details",
                        "arguments": {"target": city["city"], "names": [option["native"]["name"]]},
                    }
                )
        if "focus" in row or "construction" in row:
            rows.append(row)
    return rows
