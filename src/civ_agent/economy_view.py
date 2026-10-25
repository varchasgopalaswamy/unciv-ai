"""Economic presentation of visible facts; complete observations remain readable."""

import copy

from civ_agent.protocol import JsonObject

FULL_READ = {"tool": "player_turn", "arguments": {"view": "full"}}
INSTRUCTIONS = (
    "The economy overview is a focused brief. It retains city decisions, empire constraints, current threats "
    "and agreements; omitted rosters, site catalogues and historical comparisons are not empty or nonexistent. "
    "Omitting view keeps the current view, including recovery reads; view=full explicitly expands it. "
    "Use player_turn(view=full) for the complete overview, victory_progress for rival comparisons, and chat_read "
    "for messages and conversation history when they could change a decision. Full owned-city details remain available."
)


def economy_overview(value: JsonObject, social: JsonObject | None = None) -> JsonObject:
    result = copy.deepcopy(value)
    earlier = result.get("economyBrief", {})
    omitted = list(earlier.get("omitted", []))
    for key in ("recentProgress", "exploration", "diplomacyOpportunities"):
        if key in result:
            result.pop(key)
            omitted.append(key)
    roster = result.get("units", [])
    result["units"] = [
        {
            key: copy.deepcopy(unit[key])
            for key in (
                "handle",
                "name",
                "military",
                "workerJob",
                "workerJobTurnsRemaining",
                "movementOrder",
                "protection",
            )
            if key in unit
        }
        for unit in roster
        if not unit.get("military")
    ]
    result["economyBrief"] = {
        "fullRead": copy.deepcopy(FULL_READ),
        "militaryUnitCount": earlier.get("militaryUnitCount", sum(bool(unit.get("military")) for unit in roster)),
        "rosterScope": "Civilian work/travel summary; full military roster and unit details remain available.",
        "agreements": {
            key: copy.deepcopy((social or {}).get(key, [])) for key in ("agreements", "offers", "consents", "trades")
        },
    }
    if isinstance(result.get("settlement"), dict):
        # Keep all native expansion/happiness constraints and Settler plans;
        # only the site catalogue and its scoring reference are deferred.
        for key in ("candidates", "blockedSites", "scoring"):
            if key in result["settlement"]:
                result["settlement"].pop(key)
                omitted.append("settlement." + key)
    feedback = result.get("decisionFeedback")
    if isinstance(feedback, dict):
        result["decisionFeedback"] = {
            key: copy.deepcopy(item) for key, item in feedback.items() if key != "sharedFacts"
        }
        result["decisionFeedback"]["choices"] = [
            {
                key: copy.deepcopy(item)
                for key, item in choice.items()
                if key
                not in {
                    "actions",
                    "actionCounts",
                    "investment",
                    "omittedEarlierActions",
                    "currentUnits",
                    "currentCities",
                    "recentSubjectProgress",
                    "observedChanges",
                    "fieldRefs",
                }
            }
            for choice in feedback.get("choices", [])
        ]
        omitted.append("decisionFeedback historical comparisons")
    result["economyBrief"]["omitted"] = list(dict.fromkeys(omitted))
    return result
