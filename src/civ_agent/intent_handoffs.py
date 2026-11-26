"""Bounded handoff reminders from delivered facts and actual command attempts.

These are advisory projections. They never grant observations, ownership,
permission, completion or a native obligation, and never execute/retry an action.
"""

import copy
import hashlib
import json

from civ_agent.protocol import JsonObject

ATTEMPT_KEY = "unexecutedActionIntents"
MAX_ATTEMPTS = 8
MAX_ROUTES = 12
MAX_CHARACTERS = 6000


def _source_turn(job):
    observed = job.observation or getattr(job, "prior_role_observation", None) or {}
    turn = observed.get("turn")
    return turn if type(turn) is int else None


def command_key(command: JsonObject) -> str:
    return hashlib.sha256(json.dumps(command, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def record_action_attempts(job, operations, receipts, *, decision_id: str):
    """Track dispatch outcomes, not achievement of an authored strategic goal."""
    if not receipts:
        return
    rows = job.memory.get(ATTEMPT_KEY, [])
    if not isinstance(rows, list):
        raise ValueError("INTENT_HANDOFF_STATE_CONFLICT")
    pending = {row["commandHash"]: copy.deepcopy(row) for row in rows}
    by_id = {row["operationId"]: row for row in receipts}
    for index, operation in enumerate(operations):
        if operation.get("tool") != "player_command":
            continue
        arguments = operation.get("arguments", {})
        command = arguments.get("command")
        if command is None and isinstance(arguments.get("kind"), str):
            command = arguments
        if not isinstance(command, dict):
            continue
        command = {
            key: value for key, value in command.items() if key not in {"intent", "reviewAfter", "initiativeDecisions"}
        }
        identity = decision_id + ":" + str(index)
        receipt = by_id.get(identity)
        if receipt is None:
            continue
        key = command_key(command)
        status = receipt.get("status")
        if status == "confirmed":
            # This only resolves an earlier dispatch failure for the same exact
            # command. Its actual native outcome still needs reconsideration.
            pending.pop(key, None)
        elif status in {"unexecuted", "rejected"}:
            body = receipt.get("result") or {}
            reason = receipt.get("reason") or body.get("error") or "OPERATION_REJECTED"
            pending.pop(key, None)
            pending[key] = {
                "operationId": identity,
                "commandHash": key,
                "role": job.purpose,
                "sourceTurn": _source_turn(job),
                "status": status,
                "reason": str(reason)[:160],
                "attemptedCommand": json.dumps(command, ensure_ascii=False, separators=(",", ":"))[:1200],
                "currentIntent": "unverified",
            }
    job.memory[ATTEMPT_KEY] = list(pending.values())[-MAX_ATTEMPTS:]


def role_handoff(job) -> JsonObject:
    return {
        "role": job.purpose,
        "sourceTurn": _source_turn(job),
        "attempts": copy.deepcopy(job.memory.get(ATTEMPT_KEY, []))[-MAX_ATTEMPTS:],
        "basis": "Actual rejected or unexecuted attempts. They may have been revised or abandoned; verify current intent and native facts. A matching confirmed dispatch removes its earlier failure, without proving the strategic outcome.",
    }


def compact_attempts(handoffs: JsonObject) -> list[JsonObject]:
    """Leave full command attempts in job state; hand off small receipt links."""
    rows = []
    for handoff in handoffs.values():
        for attempt in handoff.get("attempts", [])[-3:]:
            row = copy.deepcopy(attempt)
            text = row.get("attemptedCommand", "")
            row["attemptedCommand"] = text[:240]
            row["omittedCommandCharacters"] = max(0, len(text) - 240)
            rows.append(row)
    return rows[-MAX_ATTEMPTS:]


def startup_handoff(reads: JsonObject, coordination: JsonObject | None) -> JsonObject | None:
    """Use only information also serialized in this exact prepared role input."""
    view = reads.get("player_turn", {})
    if not isinstance(view, dict):
        return None
    units = view.get("map", {}).get("units", view.get("units", []))
    routes = []
    for unit in units if isinstance(units, list) else []:
        if not isinstance(unit, dict) or unit.get("military") is not False:
            continue
        # Available order lists are options, not standing commitments.
        order = unit.get("movementOrder")
        if not order and not unit.get("settlement") and not unit.get("protection"):
            continue
        routes.append(
            {
                "unit": unit.get("handle"),
                "name": unit.get("name"),
                "position": unit.get("position"),
                "movement": unit.get("movement"),
                "standingOrder": copy.deepcopy(order),
                **{
                    key: copy.deepcopy(unit[key])
                    for key in ("protection", "escort", "afterMove", "settlement")
                    if key in unit
                },
                "source": "reads.player_turn",
                "sourceTurn": view.get("turn"),
            }
        )
    handoffs = copy.deepcopy((coordination or {}).get("intentHandoffs", []))
    if not routes and not handoffs:
        return None
    value: JsonObject = {
        "asOfTurn": view.get("turn"),
        "civilianRoutes": routes[:MAX_ROUTES],
        "unexecutedIntentHandoffs": handoffs,
        "omittedCivilianRoutes": max(0, len(routes) - MAX_ROUTES),
        "omittedIntentHandoffs": (coordination or {}).get("omittedIntentHandoffs", 0),
        "basis": "Advisory index of facts already in this input and dated command attempts. Standing routes may continue before the next assessment. Recheck relevant native orders, afterMove exposure and escort availability; choose to continue, change, wait or accept risk. No permission, inspection credit or completion comes from this index.",
    }
    # Under load, preserve explicit omission instead of hiding earlier work.
    while len(json.dumps(value, ensure_ascii=False)) > MAX_CHARACTERS and value["unexecutedIntentHandoffs"]:
        value["unexecutedIntentHandoffs"].pop(0)
        value["omittedIntentHandoffs"] += 1
    while len(json.dumps(value, ensure_ascii=False)) > MAX_CHARACTERS and value["civilianRoutes"]:
        value["civilianRoutes"].pop()
        value["omittedCivilianRoutes"] += 1
    return value
