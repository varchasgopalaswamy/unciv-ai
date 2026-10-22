"""Dated player choices and factual feedback, without strategic adjudication."""

import copy
from collections import Counter

from civ_agent.progress_history import investment_accounting
from civ_agent.protocol import JsonObject
from civ_agent.review_context import development_changes, review_position

MAX_ACTIONS = 8
MAX_CHOICES = 6


def intent_feedback(pinned: JsonObject) -> JsonObject:
    unverified = {
        key: pinned[key]
        for key in ("unverifiedStrategy", "unverifiedDecision", "unverifiedInitiatives")
        if key in pinned
    }
    if not unverified:
        return {}
    return {
        "intentFeedback": {
            "linked": False,
            **unverified,
            "instruction": "These references could not be verified; this does not affect the action. Copy saved strategy/decision IDs and review initiative numbers, or omit them. Do not repeat an action to change its annotations.",
        }
    }


def decision_baselines(content: JsonObject, previous: JsonObject, snapshot: JsonObject, turn: int | None) -> JsonObject:
    """Retain a choice's original baseline until the player changes that choice."""
    old = {row["id"]: row for row in previous.get("decisions", [])}
    baselines = previous.get("_decisionBaselines", {})
    record = snapshot.get("records", {}).get("observation:overview")
    delivered = snapshot.get("delivered", {}).get("observation:overview")
    position = (
        review_position(record["body"])
        if record and delivered == record["version"] and record["body"].get("turn") == turn
        else None
    )
    return {
        row["id"]: copy.deepcopy(baselines[row["id"]])
        if old.get(row["id"]) == row and row["id"] in baselines
        else {"authoredTurn": turn, "position": copy.deepcopy(position)}
        for row in content.get("decisions", [])
    }


def choice_versions(records: list[JsonObject], since_turn: int) -> list[JsonObject]:
    """Latest records first, from reachable history only; include recently ended choices."""
    seen, later_turns, choices = set(), {}, []
    for record in records:
        key, body = record["key"], record["body"]
        latest = key not in later_turns
        ended = body.get("updatedTurn") if latest else later_turns[key]
        active = latest and body.get("state") == "active"
        for decision in body.get("decisions", []):
            baseline = body.get("_decisionBaselines", {}).get(decision["id"], {})
            identity = baseline.get("id")
            if identity in seen:
                continue
            seen.add(identity)
            if active or (ended is not None and ended >= since_turn):
                choices.append(
                    {
                        "strategy": key,
                        "version": record["version"],
                        "decision": decision,
                        "baseline": baseline,
                        "state": "active" if active else "ended",
                        "endedTurn": None if active else ended,
                    }
                )
        later_turns[key] = body.get("updatedTurn")
    return sorted(choices, key=lambda choice: choice["state"] != "active")


def decision_feedback(
    choices: list[JsonObject], snapshot: JsonObject, actions: list[JsonObject], progress: JsonObject | None = None
) -> JsonObject:
    """Use own records and a permitted snapshot; unknown outcomes stay unknown."""
    now = review_position(snapshot)
    feedback = []
    for choice in choices[:MAX_CHOICES]:
        key, decision, baseline = choice["strategy"], choice["decision"], choice["baseline"]
        authored = baseline.get("authoredTurn")
        before = baseline.get("position")
        due = authored + decision["reviewAfterTurns"] if authored is not None else None
        subjects = {subject.casefold() for subject in decision.get("subjects", [])}
        matching = [
            action
            for action in actions
            if (action.get("_pinned") or {}).get("decision")
            == {"strategy": key, "id": decision["id"], "baseline": baseline.get("id")}
        ]
        row = {
            "strategy": key,
            "version": choice["version"],
            "state": choice["state"],
            "endedTurn": choice["endedTurn"],
            **copy.deepcopy(decision),
            "baseline": baseline.get("id"),
            "authoredTurn": authored,
            "reviewTurn": due,
            "reviewDue": choice["state"] == "active" and due is not None and now["turn"] >= due,
            "actions": [
                {field: action[field] for field in ("turn", "action", "commandStatus", "receipt") if field in action}
                for action in matching[-MAX_ACTIONS:]
            ],
            "actionCounts": dict(Counter(action["commandStatus"] for action in matching)),
            "investment": investment_accounting(matching),
            "omittedEarlierActions": max(0, len(matching) - MAX_ACTIONS),
            "currentCities": {
                handle: city
                for handle, city in now["cities"].items()
                if subjects & {handle.casefold(), city["name"].casefold()}
            },
        }
        if progress:
            targets = subjects | {(a.get("command", {}).get("target") or "").casefold() for a in matching}
            row["recentSubjectProgress"] = {
                "fromTurn": progress["fromTurn"],
                "toTurn": progress["toTurn"],
                "units": [
                    u
                    for u in progress.get("units", [])
                    if u["unit"].casefold() in targets or u["name"].casefold() in targets
                ],
                "cities": [
                    c
                    for c in progress.get("cities", [])
                    if c["city"].casefold() in targets or c["name"].casefold() in targets
                ],
                "changedFacts": [f for f in progress.get("changedFacts", []) if f["subject"].casefold() in targets],
            }
            row["currentUnits"] = [
                {key: unit.get(key) for key in ("handle", "name", "position", "military", "health")}
                for unit in snapshot.get("map", {}).get("units", [])
                if unit["handle"].casefold() in targets or unit["name"].casefold() in targets
            ]
        if before and before["turn"] < now["turn"]:
            row["observedChanges"] = development_changes(before, now)
        else:
            row["observedChanges"] = {"available": False, "reason": "No earlier delivered position"}
        feedback.append(row)
    return {
        "asOfTurn": now["turn"],
        "choices": feedback,
        "omittedChoices": max(0, len(choices) - MAX_CHOICES),
        "basis": (
            "These are your earlier expectations. Changes cover your whole position and may follow an ended choice; "
            "they do not show what caused them. Listed actions are those you linked to the choice, and acceptance "
            "does not prove success. Missing evidence is inconclusive. Review dates are reminders, not restrictions."
        ),
    }
