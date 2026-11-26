"""Bounded working context; original authored records remain in the war store."""

import copy
import json

from civ_agent.protocol import JsonObject

MAX_COORDINATION_CHARACTERS = 12000
HISTORY_PAGE_CHARACTERS = 12000

INSTRUCTIONS = (
    "Coordination contains a bounded working set for your role, not the historical archive. "
    "history.omitted counts records left out of this snapshot; retrieve relevant records with war_goals "
    "action=read, section, cursor and limit. No omitted record is completed or acknowledged. "
    "Acknowledge addressed messages with role_message action=acknowledge and ids after reading them. "
    "Acknowledging a request does not resolve it: use action=resolve with ids and a reason when addressed. "
    "Record lasting instructions in current goals, an active operation or an economy request. "
    "Military must reconcile legacy operations explicitly: read section=operations, status=legacy, "
    "then military_operation action=set_status with ids, status and reason. Age never establishes completion. "
    "Reuse stable operation IDs and update them; close or supersede obsolete plans instead of adding turn journals. "
    "Only economyWait.state=waiting arms inference suspension. attention, resolved, idle and not_running "
    "are immediate replies: continue handling the result rather than returning a final waiting message."
)
COMMUNICATION_INSTRUCTIONS = (
    "Coordination preloads bounded accepted war goals. pendingGoalRevisions identifies proposed changes, "
    "which do not replace accepted objectives. Compare dated plans with current native relationships. history.omitted counts records "
    "left out; war_goals action=read with section, IDs and cursor retrieves them. Omission never completes or "
    "acknowledges work. Main and Military own goal changes."
)


def encoded(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def current_episode(episode: JsonObject) -> JsonObject:
    result = {k: copy.deepcopy(v) for k, v in episode.items() if k not in {"history", "proposals"}}
    result["proposals"] = {
        k: copy.deepcopy(v)
        for k, v in episode.get("proposals", {}).items()
        if not v.get("finalized") and v.get("version") == episode.get("version")
    }
    return result


def relevant_messages(state: JsonObject, role: str) -> list[JsonObject]:
    messages = [
        message
        for message in state.get("messages", [])
        if (
            message.get("to") == role
            and not message.get("acknowledged", False)
            or message.get("kind") == "request"
            and not message.get("resolved", False)
            and role in {message.get("to"), message.get("author")}
        )
    ]
    # A fresh question must displace archived notices in a bounded view. Inbox
    # messages precede requests authored by this role; neither is acknowledged
    # or resolved by this ordering.
    return sorted(reversed(messages), key=lambda message: message.get("to") != role)


def working_context(context: JsonObject, role: str) -> JsonObject:
    """Keep exact rows when they fit, with counts and reads for everything else.

    The reserve covers snapshot delivery metadata. Collection budgets prevent a
    large message backlog from crowding out current goals or pending requests.
    """
    state = context.get("warState") or {}
    episodes = {k: current_episode(v) for k, v in state.get("episodes", {}).items() if v.get("active", True)}
    if role == "communication":
        for key, episode in episodes.items():
            proposals = episode.pop("proposals")
            if proposals:
                episode["pendingGoalRevisions"] = {
                    "count": len(proposals),
                    "read": {"tool": "war_goals", "arguments": {"action": "read", "section": "episodes", "ids": [key]}},
                }
    operations = {k: v for k, v in state.get("operations", {}).items() if v.get("status", "legacy") == "active"}
    messages = relevant_messages(state, role)
    requests = [r for r in context.get("requests", []) if role != "military" or r.get("author") == "military"]
    # Urgent and unresolved requests precede dispositions retained for this turn.
    requests = sorted(
        requests, key=lambda r: (r.get("status") not in {"pending", "deferred"}, r.get("priority") != "urgent")
    )
    collections = {
        "episodes": episodes if role in {"gameplay", "military", "communication"} else {},
        "drafts": state.get("drafts", {}) if role in {"gameplay", "military"} else {},
        "requests": requests,
        "messages": messages,
        "operations": operations if role in {"gameplay", "military"} else {},
        "escorts": state.get("escorts", {}) if role in {"gameplay", "military"} else {},
        "cityOverrides": context.get("cityOverrides", {}) if role in {"gameplay", "economy"} else {},
    }
    result: JsonObject = {
        "contextVersion": 2,
        "role": role,
        "rolesFinished": copy.deepcopy(context.get("rolesFinished", {})),
        "militaryActive": context.get("militaryActive", False),
        "militaryAtWar": context.get("militaryAtWar", False),
        "requests": [],
        "cityOverrides": {},
        "intentHandoffs": [],
        "omittedIntentHandoffs": 0,
        "warState": {"episodes": {}, "drafts": {}, "escorts": {}, "operations": {}, "messages": []},
        "history": {
            "tool": "war_goals",
            "arguments": {"action": "read", "section": "messages", "cursor": 0},
            "counts": {name: len(rows) for name, rows in collections.items()},
            "legacyOperations": sum(
                v.get("status", "legacy") == "legacy" for v in state.get("operations", {}).values()
            ),
            "omitted": {name: len(rows) for name, rows in collections.items()},
        },
    }
    # These are scheduling facts, never the scheduler's stored planning baseline.
    for name in ("militarySchedule", "economySchedule"):
        status = context.get(name)
        if status is not None:
            brief = {
                k: copy.deepcopy(v)
                for k, v in status.items()
                if k in {"phase", "nextReviewTurn", "fullReview", "cities"}
            }
            if len(encoded(brief)) <= 600:
                result[name] = brief
    quotas = {
        "episodes": 3000,
        "drafts": 800,
        "requests": 2400,
        "messages": 2600,
        "operations": 2200,
        "escorts": 700,
        "cityOverrides": 600,
    }
    limit = MAX_COORDINATION_CHARACTERS - 512
    for name, rows in collections.items():
        destination = result[name] if name in {"requests", "cityOverrides"} else result["warState"][name]
        used = 0
        for key, row in rows.items() if isinstance(rows, dict) else enumerate(rows):
            cost = len(encoded([key, row]))
            if used + cost > quotas[name]:
                continue
            if isinstance(destination, dict):
                destination[key] = copy.deepcopy(row)
            else:
                destination.append(copy.deepcopy(row))
            if len(encoded(result)) > limit:
                if isinstance(destination, dict):
                    del destination[key]
                else:
                    destination.pop()
                continue
            used += cost
            result["history"]["omitted"][name] -= 1
    result["history"]["omitted"] = {k: v for k, v in result["history"]["omitted"].items() if v}
    # This advisory attempt index is not a WarStore history collection. Do not
    # advertise a war_goals history read for omitted private gateway attempts.
    attempts = context.get("intentHandoffs", []) if role != "communication" else []
    used = 0
    for row in attempts:
        size = len(encoded(row))
        if used + size > 1800:
            result["omittedIntentHandoffs"] += 1
            continue
        result["intentHandoffs"].append(copy.deepcopy(row))
        if len(encoded(result)) > limit:
            result["intentHandoffs"].pop()
            result["omittedIntentHandoffs"] += 1
            continue
        used += size
    return result


def history_page(rows: list[JsonObject], cursor: int, limit: int) -> JsonObject:
    """Page exact records; explicitly identify a single unusually large record."""
    selected: list[JsonObject] = []
    for row in rows[cursor : cursor + limit]:
        if selected and len(encoded([*selected, row])) > HISTORY_PAGE_CHARACTERS:
            break
        selected.append(copy.deepcopy(row))
    following = cursor + len(selected)
    return {
        "items": selected,
        "total": len(rows),
        "nextCursor": following if following < len(rows) else None,
        "oversizedRecord": len(encoded(selected)) > HISTORY_PAGE_CHARACTERS,
    }


def control_roles(roles: JsonObject) -> JsonObject:
    """The supervisor needs lifecycle and counts, never authored coordination."""
    state = roles.get("warState") or {}
    return {
        "ready": copy.deepcopy(roles.get("ready", {})),
        "committed": roles.get("committed", False),
        "economyCompletions": roles.get("economyCompletions", 0),
        "counts": {key: len(state.get(key, {})) for key in ("episodes", "operations", "messages")},
    }
