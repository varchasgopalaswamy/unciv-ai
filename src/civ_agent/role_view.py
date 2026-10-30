"""Focused presentations of permitted facts, independent of native legality.

Projection always starts from the complete overview. It never projects a previous
brief, and leaves the gateway's raw observation available to every detail tool.
"""

import copy

from civ_agent.protocol import JsonObject

FULL_READ = {"tool": "player_turn", "arguments": {"view": "full"}}
INSTRUCTIONS = (
    "roleBrief describes a focused view, not the complete position. Omitted facts are unknown here, not absent. "
    "Omitting view preserves the selection on reads and recovery; view=full expands it. Use the listed detail "
    "reads for omitted information. Ownership labels beside commands describe role authority, not native legality; "
    "copy only the command arguments. Requests go to the named role; Main may use city_override for an emergency."
)
COMMUNICATION_INSTRUCTIONS = (
    "player_turn defaults to a communication view: empire constraints, diplomacy, city and force summaries, "
    "visible threats and uncertainty. roleBrief lists omitted information and focused reads; omitted facts are "
    "unknown, not absent. Omit view to keep the selection, or use view=full for all permitted observations. "
    "These observations do not grant authority to execute gameplay commands."
)

# Decisions, threats, constraints and uncertainty survive every view.
COMMON = frozenset(
    "turn phase canEndTurn turnControls pending alerts notifications combatReports rules scenario result "
    "economy outputSummary diplomacy cityStates socialAdvisories tacticalReview changesSincePreviousTurn "
    "mapSummary evidence".split()
)
FIELDS = {
    "main": COMMON
    | frozenset(
        "civilizationProfile eraProgress victory researchQueue research culture greatPeople researchOptions "
        "policyOptions greatPersonOptions freeTechnologies freePolicies freeGreatPeople mayaLimited "
        "exploration settlement decisionFeedback diplomacyOpportunities".split()
    ),
    "economy": COMMON
    | frozenset(
        "productionPolicy researchQueue research culture settlement wonderOpportunities constructionEstimateBasis".split()
    ),
    "military": COMMON | frozenset("militaryReadiness researchQueue".split()),
    "communication": COMMON
    | frozenset("civilizationProfile eraProgress victory researchQueue research militaryReadiness".split()),
}
CITY_SUMMARY = frozenset(
    "handle name position population health maxHealth defensiveStrength foodPerTurn turnsToGrowth "
    "turnsToStarvation constructionQueue puppet inResistance resistanceTurns beingRazed yields "
    "attacksRemaining attackOptionCount".split()
)
UNIT_SUMMARY = frozenset(
    "handle name position health movement military idle movementSupported legalDestinations attackOptionCount "
    "attacksRemaining canBuildImprovements workerJob workerJobTurnsRemaining movementOrder protection escort statuses".split()
)
COMMUNICATION_UNIT_SUMMARY = UNIT_SUMMARY | frozenset(
    "strength rangedStrength role transported carriedUnits interceptionReady".split()
)
COMMUNICATION_READINESS = frozenset(
    "roleCounts unitsExcludingRecon visibleBorderForces visibleForeignCities visibleForeignUnits "
    "omittedVisibleForeignCities omittedVisibleForeignUnits foreignObservationLimits limits".split()
)


def select(value, fields):
    return {key: copy.deepcopy(value[key]) for key in fields if key in value}


def role_overview(value, view, *, roles=None, purpose="gameplay", social=None):
    unmanaged_owner = "gameplay" if purpose == "communication" else purpose
    if view == "full":
        result = copy.deepcopy(value)
    else:
        result = select(value, FIELDS[view])
        omitted = sorted(set(value) - FIELDS[view] - {"cities", "units"})
        roster = value.get("units", [])
        result["units"] = []
        for unit in roster:
            owner = roles.unit_owner(unit) if roles is not None else unmanaged_owner
            actionable = owner == {"main": "gameplay", "economy": "economy", "military": "military"}.get(view)
            if actionable or not unit.get("military") or view == "communication":
                fields = COMMUNICATION_UNIT_SUMMARY if view == "communication" else UNIT_SUMMARY
                row = copy.deepcopy(unit) if actionable else select(unit, fields)
                row["owner"] = owner
                result["units"].append(row)
        if result["units"] != roster:
            omitted.append(
                "units: detailed orders, upgrades and action options"
                if view == "communication"
                else "units: other-role roster/options"
            )
        result["cities"] = []
        for city in value.get("cities", []):
            owner = roles.owner(city["handle"]) if roles is not None else unmanaged_owner
            actionable = owner == {"main": "gameplay", "economy": "economy"}.get(view)
            row = copy.deepcopy(city) if actionable else select(city, CITY_SUMMARY)
            row["owner"] = owner
            result["cities"].append(row)
        if result["cities"] != value.get("cities", []):
            omitted.append("cities: other-role economic options")
        if view == "economy" and isinstance(result.get("settlement"), dict):
            for key in ("candidates", "blockedSites", "scoring"):
                if key in result["settlement"]:
                    result["settlement"].pop(key)
                    omitted.append("settlement." + key)
        if view in {"economy", "military"}:
            # Native diplomacy constraints remain; option catalogues can be large.
            result["diplomacy"] = [
                select(row, {"civilizationId", "civilizationName", "status", "atWar", "peaceTreatyTurns", "bilateral"})
                for row in value.get("diplomacy", [])
            ]
            result["cityStates"] = [
                select(row, {"civilizationId", "name", "atWar", "defeated", "relationship", "ally", "resources"})
                for row in value.get("cityStates", [])
            ]
            omitted.extend(["diplomacy.native", "cityStates options"])
        elif view in {"main", "communication"}:
            result["cityStates"] = [
                select(
                    row,
                    {
                        "civilizationId",
                        "name",
                        "type",
                        "atWar",
                        "defeated",
                        "relationship",
                        "influence",
                        "decayPerTurn",
                        "turnsToRelationshipChange",
                        "ally",
                        "protecting",
                        "resources",
                    },
                )
                for row in value.get("cityStates", [])
            ]
            omitted.append("cityStates quests, bonuses and detailed options (player_details section=diplomacy)")
        if view == "economy":
            result.pop("wonderOpportunities", None)
            result["availableWonders"] = [
                row["name"]
                for row in value.get("wonderOpportunities", [])
                if any(
                    not city.get("queued") and not city.get("queueUnavailableReasons") for city in row.get("cities", [])
                )
            ]
            omitted.append(
                "wonderOpportunities: available names only; construction_details target=<city> names=[<wonder>] has details; full also includes queued and blocked wonders"
            )
        if view == "communication":
            if "development" in result.get("economy", {}):
                result["economy"].pop("development")
                omitted.append("economy.development (player_details section=economy)")
            if value.get("militaryReadiness") is not None:
                result["militaryReadiness"] = select(value["militaryReadiness"], COMMUNICATION_READINESS)
                omitted.extend(
                    "militaryReadiness." + field
                    for field in sorted(set(value["militaryReadiness"]) - COMMUNICATION_READINESS)
                )
        elif view != "military" and value.get("militaryReadiness") is not None:
            result["militaryReadiness"] = select(
                value["militaryReadiness"], {"visibleBorderForces", "foreignObservationLimits"}
            )
            if "militaryReadiness" in omitted:
                omitted.remove("militaryReadiness")
                omitted.append("militaryReadiness: other-role force and supply details")
        result["roleBrief"] = {
            "view": view,
            "omitted": omitted,
            "ownedUnits": len(roster),
            "fullRead": copy.deepcopy(FULL_READ),
            "focusedReads": {
                "cities": {"tool": "city_details", "arguments": {"target": "<city handle>"}},
                "units": {"tool": "unit_details", "arguments": {"target": "<unit handle>"}},
                "terrain": {"tool": "map_region", "arguments": {"center": "<position>"}},
                "diplomacy": {"tool": "player_details", "arguments": {"section": "diplomacy"}},
                "history": {"tool": "history_read", "arguments": {"turn": "<turn>"}},
                "victory": {"tool": "victory_progress", "arguments": {}},
                "social": {"tool": "chat_read", "arguments": {}},
                **(
                    {
                        "economy": {"tool": "player_details", "arguments": {"section": "economy"}},
                        "objectives": {"tool": "war_goals", "arguments": {"action": "read"}},
                    }
                    if view == "communication"
                    else {}
                ),
            },
        }
        if view == "economy":
            # Keep this existing field readable by older saved contexts/clients.
            result["economyBrief"] = {
                "militaryUnitCount": sum(bool(u.get("military")) for u in roster),
                "agreements": select(social or {}, {"agreements", "offers", "consents", "trades"}),
            }
    if roles is not None or purpose == "communication":
        result.pop("endTurn", None)
        controls = result.get("turnControls") or {}
        for key in ("deferChoicesCommand", "moveAutomatedCommand", "autoplayCommands"):
            controls.pop(key, None)
        if purpose != "communication":
            result["roleCompletion"] = {"tool": "finish_role", "arguments": {}}
    return result


def label_commands(value, roles, purpose, observation):
    """Add metadata at offer wrappers, never inside copyable argument objects."""
    from types import SimpleNamespace

    from civ_agent.economy_roles import CITY_COMMANDS

    job = SimpleNamespace(purpose=purpose, observation=observation)

    def access(command):
        problem = roles.ownership_problem(job, command)
        if command["kind"] == "END_TURN":
            return {"mode": "host", "tool": "finish_role"}
        if problem is None:
            return {"mode": "direct", "owner": purpose}
        owner = problem.get("owner")
        if owner is None and problem["error"] == "ROLE_ACTION_NOT_PERMITTED":
            owner = next(
                (
                    role
                    for role in ("gameplay", "economy", "military")
                    if roles.ownership_problem(SimpleNamespace(purpose=role, observation=observation), command) is None
                ),
                None,
            )
        result = {
            "mode": "request" if owner and purpose != "communication" else "unavailable",
            "reason": problem["error"],
        }
        if owner:
            result["owner"] = owner
            if purpose != "communication":
                result["tool"] = "economy_request" if owner == "economy" else "role_message"
        if purpose == "gameplay" and owner == "economy" and command["kind"] in CITY_COMMANDS:
            result["emergencyOverride"] = {"tool": "city_override", "city": command.get("target")}
        return result

    def visit(node):
        if isinstance(node, list):
            for item in node:
                visit(item)
        elif isinstance(node, dict):
            for key, item in list(node.items()):
                if (
                    isinstance(item, dict)
                    and isinstance(item.get("kind"), str)
                    and ("command" in key.lower() or key == "nativeAction")
                ):
                    node[key + "Access"] = access(item)
                elif isinstance(item, list) and "command" in key.lower():
                    node[key + "Access"] = [
                        access(command) for command in item if isinstance(command, dict) and "kind" in command
                    ]
                elif key not in {
                    "receipt",
                    "actions",
                    "history",
                    "strategicReview",
                    "records",
                    "memoryUpdates",
                    "messages",
                    "agreements",
                    "offers",
                    "consents",
                    "trades",
                }:
                    visit(item)

    result: JsonObject = copy.deepcopy(value)
    visit(result)
    return result
