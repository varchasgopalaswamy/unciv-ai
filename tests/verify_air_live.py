"""Audit native air missions, transport, nuclear effects and real model receipts."""

import argparse
import json
from pathlib import Path

from civ_agent.monitoring import path_for, read_record
from civ_agent.protocol import JsonObject

PLAYERS = ("Rome", "Greece", "France")


def verify(root: Path) -> JsonObject:
    initial = json.loads((root / "initial-game.json").read_text())
    assert initial["tileMap"]["mapParameters"]["name"] == "Air warfare acceptance"
    checkpoint = json.loads((root / "exploration.json").read_text())
    game = json.loads(checkpoint["gameJson"])
    run = read_record(path_for(root), "state", "run") or {}
    before_restart = json.loads((root / "before-restart.json").read_text())
    old_game = json.loads(before_restart["gameJson"])
    checks = {}
    receipts = {}
    settings = []
    interrupted = []
    communication_phases = {}
    for player in PLAYERS:
        service = root / "agent-services" / player
        activations = [json.loads(path.read_text()) for path in (service / "activations").glob("*.json")]
        settings.extend(row["effectiveSettings"] for row in activations)
        for path in (service / "chat-jobs").glob("*.json"):
            chat = json.loads(path.read_text())
            settings.append(chat["effectiveSettings"])
            phase = chat["phase"]
            communication_phases[phase] = communication_phases.get(phase, 0) + 1
        completed = [row for row in activations if row["phase"] == "COMPLETED"]
        interrupted.extend(
            {"player": player, "turn": row["gameTurn"], "error": row.get("error")}
            for row in activations
            if row["phase"] == "FAILED" and row.get("committedHandoff") is None
        )
        checks[f"{player}_three_completed_model_turns"] = (
            {row["gameTurn"] for row in completed} == {0, 1, 2}
            and len(completed) == 3
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
        item == {"provider": "openai", "model": "gpt-5.6-luna", "reasoningEffort": "medium"} for item in settings
    )
    checks["three_rounds_nine_handoffs"] = game.get("turns", 0) == 3 and checkpoint["handoff"] == 9
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
        result = []
        for tile in saved_game["tileMap"]["tileList"]:
            position = tile.get("position", {})  # Native JSON omits the default origin.
            for field in ("militaryUnit", "civilianUnit"):
                if unit := tile.get(field):
                    result.append((position, unit))
            result.extend((position, unit) for unit in tile.get("airUnits", []))
        return result

    def named(saved_game, name):
        return [(position, unit) for position, unit in units(saved_game) if unit.get("instanceName") == name]

    rebases = actions("Rome", "REBASE_UNIT")
    checks["fighter_rebased_onto_carrier"] = any(
        coordinate(row["request"]["destination"], -3, -2)
        and row["receipt"]["mission"]["transported"]
        and coordinate(row["receipt"]["mission"]["end"], -3, -2)
        for row in rebases
    )
    checks["carrier_and_aircraft_travelled_together_before_restart"] = all(
        len(found := named(old_game, name)) == 1 and coordinate(found[0][0], 0, -2)
        for name in ("Carrier", "Carrier wing")
    ) and named(old_game, "Carrier wing")[0][1].get("isTransported", False)
    checks["carrier_used_previewed_destination_order"] = any(
        coordinate(row["request"]["destination"], 0, -2) and coordinate(row["receipt"]["movement"]["end"], 0, -2)
        for row in actions("Rome", "SET_UNIT_DESTINATION")
    )
    checks["fighter_rebased_back_to_city_after_restart"] = any(
        coordinate(row["request"]["destination"], -1, 0) and row["receipt"]["mission"]["transported"] is False
        for row in rebases
    ) and any(
        coordinate(position, -1, 0) and not unit.get("isTransported", False)
        for position, unit in named(game, "Carrier wing")
    )
    checks["aircraft_upgrade_paid"] = any(
        row["request"]["selection"] == "Jet Fighter" and row["receipt"]["economy"]["goldChange"] < 0
        for row in actions("Rome", "UPGRADE_UNIT")
    ) and any(unit["name"] == "Jet Fighter" for _, unit in named(game, "Reserve wing"))
    checks["aircraft_disband_refunded"] = any(
        row["receipt"]["economy"]["goldChange"] > 0 for row in actions("Rome", "DISBAND_UNIT")
    ) and not named(game, "Retired bomber")
    checks["air_strike_was_intercepted"] = any(
        coordinate(row["request"]["destination"], 3, 0)
        and row["receipt"]["combat"]["damageToAttacker"] > 0
        and row["receipt"]["combat"]["damageToDefender"] > 0
        for row in actions("Rome", "ATTACK")
    ) and any(
        report["kind"] == "ObservedInterception"
        and (report.get("ourUnit") or {}).get("instanceName") == "Strike wing"
        and report["ourDamage"] > 0
        for report in checkpoint["players"]["Rome"].get("combatReports", {}).values()
    )
    checks["sweep_engaged_interceptor"] = any(
        row["receipt"]["mission"]["resolution"] == "Completed"
        and any(
            report["kind"] == "ObservedInterception" and report["ourDamage"] > 0
            for report in row["receipt"]["mission"]["reports"]
        )
        for row in actions("Rome", "AIR_SWEEP")
    )
    checks["guided_missile_consumed_after_strike"] = any(
        coordinate(row["request"]["destination"], 3, 1)
        and row["receipt"]["combat"]["attackerDestroyed"]
        and row["receipt"]["combat"]["damageToDefender"] > 0
        for row in actions("Rome", "ATTACK")
    ) and not named(game, "Guided strike")
    wing_ids = {row["request"]["target"] for row in rebases if coordinate(row["request"]["destination"], -3, -2)}
    checks["carrier_aircraft_attacked_after_restart"] = any(
        row["request"]["target"] in wing_ids and row["receipt"]["turn"] == 1 and row["receipt"]["combat"]["attacked"]
        for row in actions("Rome", "ATTACK")
    )
    checks["paradrop_reached_selected_tile"] = any(
        coordinate(row["receipt"]["mission"]["end"], -3, 6) for row in actions("France", "PARADROP")
    ) and any(coordinate(position, -3, 6) for position, _ in named(game, "Drop team"))
    checks["nuclear_weapon_retained_through_restart"] = bool(named(old_game, "Nuclear trial"))
    launches = actions("France", "NUCLEAR_STRIKE")
    initial_greece = next(civ for civ in initial["civilizations"] if civ["civID"] == "Greece")
    final_greece = next(civ for civ in game["civilizations"] if civ["civID"] == "Greece")
    final_france = next(civ for civ in game["civilizations"] if civ["civID"] == "France")
    checks["confirmed_nuclear_detonation_consumed_weapon"] = (
        len(launches) == 1
        and all(
            row["request"].get("confirmAction")
            and coordinate(row["request"]["destination"], 6, 6)
            and row["receipt"]["mission"]["resolution"] == "Completed"
            and row["receipt"]["mission"]["destroyed"]
            for row in launches
        )
        and not named(game, "Nuclear trial")
    )
    checks["nuclear_launch_caused_native_war"] = any(
        "Greece" in row["receipt"]["mission"]["newWars"] for row in launches
    ) and (
        not actions("France", "DECLARE_WAR")
        and initial_greece["diplomacy"]["France"].get("diplomaticStatus", "War") == "Peace"
        # Native serialization omits the default status, which is War.
        and final_greece["diplomacy"]["France"].get("diplomaticStatus", "War") == "War"
        and final_france["diplomacy"]["Greece"].get("diplomaticStatus", "War") == "War"
    )
    checks["native_nuclear_casualty_and_fallout"] = not named(game, "Nuclear target") and any(
        "Fallout" in tile.get("terrainFeatures", []) for tile in game["tileMap"]["tileList"]
    )
    checks["greece_received_nuclear_report"] = any(
        report["kind"] == "ObservedNuclearDetonation"
        for report in checkpoint["players"]["Greece"].get("combatReports", {}).values()
    )
    checks["paused_at_run_limit"] = (
        checkpoint["paused"]
        and run["status"] == "budget_exhausted"
        and run["reason"] == "Game-turn budget exhausted."
        and run["limits"]["maxTurns"] == 3
        and run["inFlight"] is None
    )
    return {
        "passed": all(checks.values()),
        "checks": checks,
        "modelJobs": len(settings),
        "interruptedActivations": interrupted,
        "communicationPhases": communication_phases,
        "handoff": checkpoint["handoff"],
        "turn": game.get("turns", 0),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", type=Path)
    args = parser.parse_args()
    result = verify(args.root)
    (args.root / "air-verification.json").write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result, indent=2))
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
