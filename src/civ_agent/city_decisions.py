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
    "Focus and construction are independent recommendations over the same captured state; neither assumes the other was applied.",
    "The gameplay agent coordinates empire needs and can accept, change or reject either choice without an override.",
    "Read the selected option's normal inspection before acting. Queue commands append; reordering is a separate native operation.",
]


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
    if job.purpose != "gameplay" or observation.get("phase") != "ACTIVE":
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
        "strategy": copy.deepcopy(job.memory.get("strategicAdvice")),
    }
    context["economy"]["resources"] = [
        {k: r.get(k) for k in ("name", "available", "used", "imported", "exported")}
        for r in observation.get("economy", {}).get("resources", [])
    ]
    # Raw snapshots carry map.units; only an owned-unit roster is needed here.
    context["units"] = [
        {k: u.get(k) for k in ("handle", "name", "military")} for u in observation.get("map", {}).get("units", [])
    ]
    words = set(re.findall(r"\w{3,}", str([objectives, context["strategy"]]).casefold()))
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
        moves = {
            p["name"]: next(command for command in p["moveCommands"] if command["toIndex"] == 0)
            for p in production.get("data", {}).get("entity", {}).get("development", {}).get("constructions", [])
            if any(command["toIndex"] == 0 for command in p.get("moveCommands", []))
        }
        all_names = set(city.get("constructionOptions", [])) | moves.keys()
        names = list(all_names)
        available_since = (city.get("constructionChoice") or {}).get("availableSinceChoice", [])
        names.sort(key=lambda n: (n not in available_since, -len(words & set(re.findall(r"\w{3,}", n.casefold()))), n))
        names = names[:CONSTRUCTION_LIMIT]
        constructions = []
        if names:
            details = project_detail(
                observation,
                DetailQuery(tool="construction_details", arguments={"target": city["handle"], "names": names}),
            )
            for option in details.get("data", {}).get("constructions", []):
                command = moves.get(option["name"]) or option.get("command")
                if command is None:
                    continue
                estimate = city.get("constructionEstimates", {}).get(option["name"])
                if estimate is None:
                    continue
                constructions.append(
                    {
                        "id": f"construction{len(constructions)}",
                        "native": {
                            "name": option["name"],
                            "command": copy.deepcopy(command),
                            "queueOperation": "move_to_front" if command["kind"] == "MOVE_CONSTRUCTION" else "append",
                            **construction_estimate(estimate),
                            **{
                                k: copy.deepcopy(option[k])
                                for k in ("queueUnavailableReasons", "progress")
                                if k in option
                            },
                            **{
                                k: copy.deepcopy(estimate[k])
                                for k in ("requiredTechnologies", "resourceRequirements")
                                if k in estimate
                            },
                        },
                    }
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
                "omittedConstructions": max(0, len(all_names) - len(constructions)),
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
            answer = answers.get(f"{prefix}:{index}")
            if not answer:
                continue
            selected = answer["choice"]
            option = next((o for o in city[candidates] if o["id"] == selected), None)
            if option is None and selected not in {"keep", "defer"}:
                continue
            row[field] = {"source": "decisions", "choice": selected, "confidence": answer["confidence"]}
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
