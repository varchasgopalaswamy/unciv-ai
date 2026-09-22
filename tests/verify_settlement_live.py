"""Audit destination advice delivered to the model and native settlement receipts."""

import argparse
import json
from pathlib import Path

from sqlalchemy import create_engine, select

from civ_agent import memory_schema as tables
from civ_agent.protocol import JsonObject
from civ_agent.state_updates import apply_overview_update
from civ_agent.tls import write_private_json


def verify(root: Path) -> JsonObject:
    initial = json.loads((root / "initial-turn.json").read_text())
    settler = next(unit for unit in initial["map"]["units"] if unit["name"] == "Settler")
    target = settler["handle"]
    checkpoint = json.loads((root / "exploration.json").read_text())
    game = json.loads(checkpoint["gameJson"])
    service = root / "agent-services" / "Rome"
    database = service / "civilization" / "memory.sqlite3"
    engine = create_engine(f"sqlite:///{database.as_uri()}?mode=ro&uri=true")
    try:
        with engine.connect() as db:
            calls = (
                db.execute(
                    select(
                        tables.jobs.c.game_turn, tables.calls.c.name, tables.calls.c.arguments, tables.calls.c.result
                    )
                    .select_from(tables.calls.join(tables.jobs, tables.calls.c.job == tables.jobs.c.id))
                    .where(tables.jobs.c.purpose == "gameplay", tables.calls.c.result.is_not(None))
                    .order_by(tables.jobs.c.game_turn, tables.call_order)
                )
                .mappings()
                .all()
            )
    finally:
        engine.dispose()

    current = None
    transitions: list[JsonObject] = []
    travel_turns = set()
    previous_unit = None
    for call in calls:
        result = json.loads(call["result"])
        source = call["name"]
        if source == "player_turn" and "units" in result:
            current = result
        elif result.get("stateUpdate", {}).get("kind") in {"full", "delta"}:
            current = apply_overview_update(current, result["stateUpdate"])
            source = "command_update"
        else:
            continue
        unit = next((unit for unit in current["units"] if unit["handle"] == target), None)
        if previous_unit and unit and unit["position"] != previous_unit["position"]:
            travel_turns.add(call["game_turn"])
        before = (previous_unit or {}).get("movementOrder", {})
        after = (unit or {}).get("movementOrder", {})
        if before != after:
            transitions.append(
                {
                    "turn": call["game_turn"],
                    "source": source,
                    "before": before,
                    "after": after,
                    "settlerStayedStill": bool(
                        previous_unit and unit and unit["position"] == previous_unit["position"]
                    ),
                }
            )
        previous_unit = unit

    receipts = checkpoint["players"]["Rome"]["receipts"]
    actions = [row for row in receipts if row["request"].get("target") == target]
    founded = next((row["receipt"]["turn"] for row in actions if row["request"]["kind"] == "FOUND_CITY"), None)
    destination = settler["movementOrder"]["destination"]
    redirects = 0
    for row in actions:
        if row["request"]["kind"] == "SET_UNIT_DESTINATION":
            requested = row["request"]["destination"]
            redirects += requested != destination
            destination = requested
        movement = row["receipt"].get("movement")
        if movement and movement["start"] != movement["end"]:
            travel_turns.add(row["receipt"]["turn"])
    activations = [json.loads(path.read_text()) for path in (service / "activations").glob("*.json")]
    checks = {
        "initial_destination_unknown": settler["movementOrder"]["settlement"]["siteStatus"] == "UNKNOWN",
        "blocker_delivered_in_action_update_while_settler_stationary": any(
            row["source"] == "command_update"
            and row["settlerStayedStill"]
            and row["before"].get("settlement", {}).get("siteStatus") == "UNKNOWN"
            and row["after"].get("settlement", {}).get("siteStatus") == "BLOCKED"
            for row in transitions
        ),
        "additional_roman_city_founded": founded is not None
        and len(next(civ for civ in game["civilizations"] if civ["civID"] == "Rome")["cities"]) == 2,
        "no_tile_by_tile_settler_moves": all(row["request"]["kind"] != "MOVE_UNIT" for row in actions),
        "all_model_turns_completed": bool(activations) and all(row["phase"] == "COMPLETED" for row in activations),
        "luna_medium": bool(activations)
        and all(
            row["effectiveSettings"] == {"model": "gpt-5.6-luna", "reasoningEffort": "medium"} for row in activations
        ),
    }
    return {
        "passed": all(checks.values()),
        "checks": checks,
        "metrics": {
            "settlerAvailableTurn": initial["turn"],
            "foundedTurn": founded,
            "turnsFromAvailabilityToFounding": founded - initial["turn"] if founded is not None else None,
            "turnsWithSettlerTravel": sorted(travel_turns),
            "stationaryTurnsBeforeFounding": [
                turn for turn in range(initial["turn"], founded) if turn not in travel_turns
            ]
            if founded is not None
            else None,
            "retargetOrders": redirects,
            "cancelledOrders": sum(row["request"]["kind"] == "CANCEL_UNIT_MOVEMENT" for row in actions),
        },
        "destinationTransitions": transitions,
        "scope": "Prepared settler available at turn zero; production time is not simulated. No speed comparison with an earlier model run.",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", type=Path)
    args = parser.parse_args()
    root = args.root.expanduser().resolve()
    result = verify(root)
    write_private_json(root / "settlement-verification.json", result)
    print(json.dumps(result, indent=2))
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
