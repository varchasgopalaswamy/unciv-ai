"""Audit native effects and archived model receipts for the guided city-state game."""

import argparse
import json
from pathlib import Path

from civ_agent.monitoring import path_for, read_record
from civ_agent.protocol import JsonObject

PLAYERS = ("Rome", "Greece", "France")


def verify(root: Path) -> JsonObject:
    initial = json.loads((root / "initial-game.json").read_text())
    assert initial["tileMap"]["mapParameters"]["name"] == "City-state live acceptance"
    checkpoint = json.loads((root / "exploration.json").read_text())
    game = json.loads(checkpoint["gameJson"])
    run = read_record(path_for(root), "state", "run") or {}
    civs = {civ["civID"]: civ for civ in game["civilizations"]}
    checks = {}
    settings = []
    activations = []
    receipts = {}
    communication_receipts = []
    for player in PLAYERS:
        service = root / "agent-services" / player
        own = [json.loads(path.read_text()) for path in (service / "activations").glob("*.json")]
        activations.extend(own)
        settings.extend(row["effectiveSettings"] for row in own)
        settings.extend(
            json.loads(path.read_text())["effectiveSettings"] for path in (service / "chat-jobs").glob("*.json")
        )
        checks[f"{player}_three_completed_model_turns"] = (
            {row["gameTurn"] for row in own} == {0, 1, 2}
            and len(own) == 3
            and all(
                row["phase"] == "COMPLETED" and row.get("threadId") and row.get("committedHandoff") is not None
                for row in own
            )
        )
        folder = root / "agents" / player
        rows = [
            json.loads(line) for path in folder.glob("turn_*/receipts.jsonl") for line in path.read_text().splitlines()
        ]
        receipts[player] = {row["receipt"]["requestId"]: row for row in rows if "requestId" in row["receipt"]}
        communication_receipts.extend(row for row in rows if "requestId" not in row["receipt"])
        saved = {row["receipt"]["requestId"]: row for row in checkpoint["players"][player]["receipts"]}
        checks[f"{player}_archive_matches_checkpoint"] = (
            bool(saved)
            and set(saved) == set(receipts[player])
            and all(saved[key]["receipt"] == receipts[player][key]["receipt"] for key in saved)
        )

    checks["all_models_use_requested_settings"] = bool(settings) and all(
        setting == {"provider": "openai", "model": "gpt-5.6-luna", "reasoningEffort": "medium"} for setting in settings
    )
    checks["three_rounds_nine_handoffs"] = game.get("turns", 0) == 3 and checkpoint["handoff"] == 9
    checks["ordinary_vanilla_city_states"] = (
        game["gameParameters"]["baseRuleset"] == "Civ V - Vanilla"
        and game["gameParameters"]["numberOfCityStates"] == 2
        and all(civs[name]["playerType"] == "Human" for name in PLAYERS)
        and all(civs[name].get("playerType", "AI") == "AI" for name in ("Geneva", "Sidon"))
    )
    checks["funded_initial_objectives"] = all(
        civ.get("gold", 0) >= 5000 for civ in initial["civilizations"] if civ["civID"] in PLAYERS
    )

    def outcomes(player, action):
        return [
            row["receipt"]["cityState"]
            for row in receipts[player].values()
            if (row["receipt"].get("cityState") or {}).get("action") == action
        ]

    roman_gifts = [
        row["receipt"]["cityState"]
        for row in receipts["Rome"].values()
        if (row["receipt"].get("cityState") or {}).get("action", "").startswith("giftGold")
    ]
    checks["rome_completed_gold_quest_and_allied"] = any(
        "Give Gold" in effect["completedOrRemovedQuests"]
        and effect["allyAfter"]["civilizationId"] == "Rome"
        and effect["goldChange"] in {-250, -500, -1000}
        for effect in roman_gifts
    )
    checks["rome_improved_gems"] = any(
        (row["receipt"].get("cityState") or {}).get("improvement") == "Mine"
        and row["receipt"]["cityState"]["goldChange"] == -200
        for row in receipts["Rome"].values()
    )
    checks["rome_gifted_unit"] = any(
        effect["unitRemoved"] and effect["influenceAfter"] == effect["influenceBefore"] + 5
        for effect in outcomes("Rome", "GIFT_UNIT")
    )
    checks["rome_conducted_mission"] = any(
        effect["unitRemoved"] and effect["goldChange"] > 0 and effect["influenceAfter"] > effect["influenceBefore"]
        for effect in outcomes("Rome", "mission1")
    )
    checks["greece_took_geneva_alliance"] = (
        any(
            (effect.get("allyBefore") or {}).get("civilizationId") == "Rome"
            and (effect.get("allyAfter") or {}).get("civilizationId") == "Greece"
            for row in receipts["Greece"].values()
            if (effect := row["receipt"].get("cityState"))
        )
        and civs["Geneva"]["allyCivName"] == "Greece"
    )
    checks["native_protection_persisted"] = all(
        civs[minor]["diplomacy"][player]["diplomaticStatus"] == "Protector"
        and civs[minor]["diplomacy"][player]["flagsCountdown"]["RecentlyPledgedProtection"] == 7
        for player, minor in (("Rome", "Geneva"), ("Greece", "Sidon"))
    )
    checks["greece_completed_protection_quest"] = (
        bool(outcomes("Greece", "pledgeProtection"))
        and not civs["Sidon"].get("questManager", {}).get("assignedQuests")
        and civs["Sidon"]["diplomacy"]["Greece"].get("influence", 0) > 10
    )
    checks["france_received_native_tribute"] = any(
        effect["goldChange"] == 60 and effect["influenceAfter"] == effect["influenceBefore"] - 15
        for effect in outcomes("France", "goldTribute")
    )
    checks["greece_responded_to_protected_city_state_incident"] = any(
        (row["receipt"].get("nativeOutcome") or {}).get("selection") == "SideWithCityState"
        and row["receipt"]["nativeOutcome"]["status"] == "ENACTED"
        and row["receipt"]["nativeOutcome"]["author"] == "Greece"
        and row["receipt"]["nativeOutcome"]["target"] == "France"
        for row in communication_receipts
    )
    checks["native_incident_consequences_persisted"] = (
        civs["Greece"]["diplomacy"]["France"]["diplomaticModifiers"].get("BulliedProtectedMinor", 0) < 0
        and civs["France"]["diplomacy"]["Greece"]["diplomaticModifiers"].get("SidedWithProtectedMinor", 0) < 0
        and civs["France"]["diplomacy"]["Greece"]["diplomaticStatus"] == "Peace"
    )
    checks["native_chat_receipts_match_checkpoint"] = bool(communication_receipts) and all(
        checkpoint["communication"]["requests"][f"{row['receipt']['message']['author']}:{row['request']['requestId']}"][
            "response"
        ]
        == row["receipt"]
        for row in communication_receipts
    )
    checks["native_influence_decay_occurred"] = (
        civs["Geneva"]["diplomacy"]["Rome"]["influence"] < outcomes("Rome", "pledgeProtection")[-1]["influenceAfter"]
    )
    checks["no_repeat_missions_or_tribute"] = (
        len(outcomes("Rome", "mission1"))
        == len(outcomes("Rome", "GIFT_UNIT"))
        == len(outcomes("France", "goldTribute"))
        == 1
    )
    baseline = json.loads((root / "before-restart.json").read_text())
    checks["first_round_receipts_survived_restart"] = (
        baseline["handoff"] == 3
        and baseline["epoch"] == checkpoint["epoch"]
        and all(
            receipts[player].get(row["receipt"]["requestId"], {}).get("receipt") == row["receipt"]
            for player in PLAYERS
            for row in baseline["players"][player]["receipts"]
        )
    )
    return {
        "passed": all(checks.values()),
        "checks": checks,
        "activations": len(activations),
        "modelJobs": len(settings),
        "turn": game.get("turns", 0),
        "handoff": checkpoint["handoff"],
        "schedulerStatus": run["status"],
        "outcomes": {
            player: [row["receipt"]["cityState"] for row in rows.values() if row["receipt"].get("cityState")]
            for player, rows in receipts.items()
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("state_dir", type=Path)
    args = parser.parse_args()
    report = verify(args.state_dir)
    (args.state_dir / "city-state-live-verification.json").write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps({key: value for key, value in report.items() if key != "outcomes"}, indent=2))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
