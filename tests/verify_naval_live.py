"""Audit native naval effects, route persistence and real model receipts."""

import argparse
import json
from pathlib import Path

from civ_agent.protocol import JsonObject

PLAYERS = ("Rome", "Greece", "France")


def verify(root: Path) -> JsonObject:
    initial = json.loads((root / "initial-game.json").read_text())
    assert initial["tileMap"]["mapParameters"]["name"] == "Naval gameplay acceptance"
    checkpoint = json.loads((root / "exploration.json").read_text())
    game = json.loads(checkpoint["gameJson"])
    before_restart = json.loads((root / "before-restart.json").read_text())
    old_game = json.loads(before_restart["gameJson"])
    civs = {civ["civID"]: civ for civ in game["civilizations"]}
    checks = {}
    receipts = {}
    settings = []
    interrupted = []
    for player in PLAYERS:
        service = root / "agent-services" / player
        activations = [json.loads(path.read_text()) for path in (service / "activations").glob("*.json")]
        settings.extend(row["effectiveSettings"] for row in activations)
        settings.extend(json.loads(p.read_text())["effectiveSettings"] for p in (service / "chat-jobs").glob("*.json"))
        completed = [row for row in activations if row["phase"] == "COMPLETED"]
        interrupted.extend(
            {"player": player, "turn": row["gameTurn"], "error": row.get("error")}
            for row in activations
            if row["phase"] == "FAILED" and row.get("committedHandoff") is None
        )
        checks[f"{player}_four_completed_model_turns"] = (
            {row["gameTurn"] for row in completed} == {0, 1, 2, 3}
            and len(completed) == 4
            and all(
                row["phase"] == "COMPLETED" and row.get("threadId") and row.get("committedHandoff") is not None
                for row in completed
            )
            and all(row["phase"] in {"COMPLETED", "FAILED"} for row in activations)
        )
        rows = [
            json.loads(line)
            for p in (root / "agents" / player).glob("turn_*/receipts.jsonl")
            for line in p.read_text().splitlines()
        ]
        receipts[player] = {row["receipt"]["requestId"]: row for row in rows if "requestId" in row["receipt"]}
        saved = {row["receipt"]["requestId"]: row for row in checkpoint["players"][player]["receipts"]}
        checks[f"{player}_archive_matches_native_receipts"] = (
            bool(saved)
            and saved.keys() == receipts[player].keys()
            and all(saved[key]["receipt"] == receipts[player][key]["receipt"] for key in saved)
        )
        old = {row["receipt"]["requestId"]: row for row in before_restart["players"][player]["receipts"]}
        checks[f"{player}_prior_receipts_preserved_across_restart"] = bool(old) and all(
            key in saved and saved[key] == value for key, value in old.items()
        )
    checks["all_game_agents_use_luna_medium"] = bool(settings) and all(
        item == {"model": "gpt-5.6-luna", "reasoningEffort": "medium"} for item in settings
    )
    checks["four_rounds_twelve_handoffs"] = game.get("turns", 0) == 4 and checkpoint["handoff"] == 12
    checks["same_epoch_across_planned_restart"] = (
        checkpoint["epoch"] == before_restart["epoch"] and before_restart["handoff"] == 3
    )
    checks["funded_starting_objectives"] = all(
        civ.get("gold", 0) >= 5000 for civ in initial["civilizations"] if civ["civID"] in PLAYERS
    )

    def actions(player, kind):
        return [row for row in receipts[player].values() if row["request"]["kind"] == kind]

    def coordinate(value, x, y):
        if isinstance(value, dict):
            return value.get("x", 0) == x and value.get("y", 0) == y
        return value in ({"x": x, "y": y}, f"{x},{y}", f"({x},{y})")

    def units(saved_game):
        return [
            (tile["position"], unit)
            for tile in saved_game["tileMap"]["tileList"]
            for field in ("militaryUnit", "civilianUnit")
            if (unit := tile.get(field))
        ]

    roman_boats = actions("Rome", "CREATE_WATER_IMPROVEMENT")
    checks["work_boat_consumed_and_whales_connected"] = any(
        (row["receipt"].get("waterImprovement") or {}).get("unitConsumed")
        and row["receipt"]["waterImprovement"]["improvementAfter"] == "Fishing Boats"
        and row["receipt"]["waterImprovement"]["availableResourceAfter"]
        > row["receipt"]["waterImprovement"]["availableResourceBefore"]
        for row in roman_boats
    )
    checks["greek_fish_improved"] = any(
        (row["receipt"].get("waterImprovement") or {}).get("unitConsumed")
        for row in actions("Greece", "CREATE_WATER_IMPROVEMENT")
    )
    checks["ship_upgrade_paid"] = any(
        row["request"]["selection"] == "Caravel" and row["receipt"]["economy"]["goldChange"] < 0
        for row in actions("Rome", "UPGRADE_UNIT")
    )
    checks["naval_attack_and_coastal_bombardment"] = all(
        any(
            coordinate(row["request"].get("destination"), x, y)
            and row["receipt"]["combat"]["attacked"]
            and row["receipt"]["combat"]["damageToDefender"] > 0
            for row in actions("Rome", "ATTACK")
        )
        for x, y in ((6, -1), (7, 0))
    )
    roman_orders = actions("Rome", "SET_UNIT_DESTINATION")
    checks["voyager_received_persistent_order"] = any(
        coordinate(row["request"].get("destination"), 1, -2) and row["receipt"]["movement"]["orderAfter"] is not None
        for row in roman_orders
    )
    checks["voyager_arrived_automatically_before_restart"] = (
        any(
            unit.get("owner") == "Rome"
            and unit.get("instanceName") == "Voyager"
            and coordinate(position, 1, -2)
            and not (unit.get("action") or "").startswith("moveTo ")
            for position, unit in units(old_game)
        )
        and len(
            [
                row
                for row in before_restart["players"]["Rome"]["receipts"]
                if row["request"].get("target")
                == next(
                    (
                        order["request"]["target"]
                        for order in roman_orders
                        if coordinate(order["request"]["destination"], 1, -2)
                    ),
                    None,
                )
                and row["request"]["kind"] in {"SET_UNIT_DESTINATION", "MOVE_UNIT"}
            ]
        )
        == 1
    )
    cancels = actions("Rome", "CANCEL_UNIT_MOVEMENT")
    checks["replacement_order_cancelled_without_movement"] = any(
        row["receipt"]["movement"]["orderBefore"] is not None
        and row["receipt"]["movement"]["orderAfter"] is None
        and row["receipt"]["movement"]["start"] == row["receipt"]["movement"]["end"]
        and row["receipt"]["movement"]["movementBefore"] == row["receipt"]["movement"]["movementAfter"]
        for row in cancels
    )
    checks["cancelled_destination_did_not_resume"] = any(
        unit.get("owner") == "Rome"
        and unit.get("instanceName") == "Voyager"
        and not (unit.get("action") or "").startswith("moveTo ")
        and any(
            coordinate(position, row["receipt"]["movement"]["end"]["x"], row["receipt"]["movement"]["end"]["y"])
            for row in cancels
        )
        for position, unit in units(game)
    )
    checks["settler_order_persisted_at_restart"] = any(
        unit.get("owner") == "France" and unit.get("name") == "Settler" and unit.get("action") == "moveTo -4,7"
        for _, unit in units(old_game)
    )
    checks["overseas_city_founded"] = any(
        coordinate(city.get("location"), -4, 7) for city in civs["France"]["cities"]
    ) and bool(actions("France", "FOUND_CITY"))
    checks["french_settler_used_destination_order"] = bool(actions("France", "SET_UNIT_DESTINATION"))
    checks["no_tile_by_tile_move_commands"] = all(not actions(player, "MOVE_UNIT") for player in PLAYERS)
    checks["france_remained_neutral"] = all(
        relation.get("diplomaticStatus", "Peace") != "War" for relation in civs["France"].get("diplomacy", {}).values()
    )
    checks["rome_and_greece_still_own_capitals"] = all(
        any(city.get("isOriginalCapital") for city in civs[name]["cities"]) for name in ("Rome", "Greece")
    )
    return {
        "passed": all(checks.values()),
        "checks": checks,
        "modelJobs": len(settings),
        "interruptedActivations": interrupted,
        "handoff": checkpoint["handoff"],
        "turn": game.get("turns", 0),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", type=Path)
    args = parser.parse_args()
    result = verify(args.root)
    (args.root / "naval-verification.json").write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result, indent=2))
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
