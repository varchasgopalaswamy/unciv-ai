"""Read-only audit of a finished live victory game's model records and native save."""

import argparse
import json
from collections import Counter
from pathlib import Path

from civ_agent.monitoring import path_for, read_record
from civ_agent.protocol import JsonObject
from civ_agent.result_schema import PlayerResult

PLAYERS = ("Rome", "Greece", "France")
PARTS = {"SS Booster": 3, "SS Cockpit": 1, "SS Engine": 1, "SS Stasis Chamber": 1}


def native_parts(value):
    if isinstance(value, dict):
        if value.get("name") in PARTS:
            yield value
        for child in value.values():
            yield from native_parts(child)
    elif isinstance(value, list):
        for child in value:
            yield from native_parts(child)


def verify(root: Path) -> JsonObject:
    initial = json.loads((root / "initial-game.json").read_text())
    kind = initial["tileMap"]["mapParameters"]["name"].removeprefix("Victory live acceptance: ")
    assert kind in {"spaceship", "election"}
    checkpoint = json.loads((root / "exploration.json").read_text())
    game = json.loads(checkpoint["gameJson"])
    run = read_record(path_for(root), "state", "run") or {}
    checks: dict[str, bool] = {}
    winner, victory_type = ("Rome", "Scientific") if kind == "spaceship" else ("Greece", "Diplomatic")
    checks["native_victory"] = (
        game.get("victoryData", {}).get("winningCiv") == winner
        and game.get("victoryData", {}).get("victoryType") == victory_type
    )
    checks["scheduler_completed_all_results"] = run["status"] == "complete" and set(
        run.get("resultsDelivered", [])
    ) == set(PLAYERS)
    checks["initial_position_has_no_victory"] = initial.get("victoryData") is None
    checks["ordinary_vanilla_all_victories"] = game["gameParameters"]["baseRuleset"] == "Civ V - Vanilla" and set(
        game["gameParameters"]["victoryTypes"]
    ) == {"Scientific", "Cultural", "Domination", "Diplomatic", "Time"}
    settings = []
    activations = []
    receipts = {}
    results = {}
    for civilization in PLAYERS:
        folder = root / "agents" / civilization
        final = PlayerResult.model_validate_json((folder / "result.json").read_text())
        results[civilization] = final
        checks[f"{civilization}_correct_final_result"] = (
            final.epoch == checkpoint["epoch"]
            and final.civilization == civilization
            and final.result.winningCivilizationId == winner
            and final.result.victoryType == victory_type
            and final.result.outcome == ("Victory" if civilization == winner else "Defeat")
        )
        service = root / "agent-services" / civilization
        own_activations = [json.loads(path.read_text()) for path in (service / "activations").glob("*.json")]
        activations.extend(own_activations)
        checks[f"{civilization}_model_turns_confirmed"] = bool(own_activations) and all(
            a.get("threadId") and (a.get("committedHandoff") is not None or a.get("terminalOutcome") is True)
            for a in own_activations
        )
        settings.extend(a["effectiveSettings"] for a in own_activations)
        settings.extend(
            json.loads(path.read_text())["effectiveSettings"] for path in (service / "chat-jobs").glob("*.json")
        )
        rows = [
            json.loads(line) for path in folder.glob("turn_*/receipts.jsonl") for line in path.read_text().splitlines()
        ]
        receipts[civilization] = {row["receipt"]["requestId"]: row for row in rows}
        saved = {row["receipt"]["requestId"]: row for row in checkpoint["players"][civilization]["receipts"]}
        checks[f"{civilization}_archive_matches_host_receipts"] = bool(saved) and (
            set(saved) == set(receipts[civilization])
            and all(saved[key]["receipt"] == receipts[civilization][key]["receipt"] for key in saved)
        )
    checks["every_model_uses_requested_settings"] = bool(settings) and all(
        setting == {"provider": "openai", "model": "gpt-5.6-luna", "reasoningEffort": "medium"} for setting in settings
    )
    checks["three_model_players"] = all(
        next(c for c in game["civilizations"] if c["civID"] == name)["playerType"] == "Human" for name in PLAYERS
    )
    checks["all_host_receipts_archived_by_agent_services"] = sum(len(rows) for rows in receipts.values()) == sum(
        len(player["receipts"]) for player in checkpoint["players"].values()
    )
    milestones = []
    if kind == "spaceship":
        before = next(c for c in initial["civilizations"] if c["civID"] == "Rome")
        after = next(c for c in game["civilizations"] if c["civID"] == "Rome")
        checks["no_prebuilt_parts_or_assembly"] = not list(native_parts(initial)) and not before.get(
            "victoryManager", {}
        ).get("currentsSpaceshipParts")
        checks["no_supplied_part_production"] = all(
            city["cityConstructions"].get("inProgressConstructions", {}).get(part, 0) == 0
            for city in before["cities"]
            for part in PARTS
        )
        rows = list(receipts["Rome"].values())
        assembly = [row for row in rows if row["receipt"]["kind"] == "ADD_SPACESHIP_PART"]
        checks["six_distinct_parts_assembled"] = (
            Counter(row["receipt"]["victory"]["part"] for row in assembly) == PARTS
            and len({row["request"]["target"] for row in assembly}) == 6
            and after["victoryManager"]["currentsSpaceshipParts"] == PARTS
            and not list(native_parts(game))
        )
        queued = [
            row
            for row in rows
            if row["request"].get("selection") in PARTS and row["receipt"]["kind"] == "QUEUE_CONSTRUCTION"
        ]
        checks["all_part_types_queued_by_model"] = {row["request"]["selection"] for row in queued} == set(PARTS)
        checks["both_cities_used_for_production"] = len({row["request"]["target"] for row in queued}) == 2
        moved = {row["request"]["target"] for row in rows if row["receipt"]["kind"] == "MOVE_UNIT"}
        checks["produced_parts_transported_before_assembly"] = any(
            row["request"]["target"] in moved for row in assembly
        )
        milestones = [
            {
                "turn": row["receipt"]["turn"],
                "part": row["receipt"]["victory"]["part"],
                "assembled": row["receipt"]["victory"]["assembled"],
            }
            for row in sorted(assembly, key=lambda row: row["receipt"]["turn"])
        ]
    else:
        for civilization in PLAYERS:
            rows = list(receipts[civilization].values())
            ballots = sorted(
                (
                    row["receipt"]
                    for row in rows
                    if row["receipt"]["kind"] in {"CAST_DIPLOMATIC_VOTE", "ABSTAIN_DIPLOMATIC_VOTE"}
                ),
                key=lambda row: row["turn"],
            )
            choices = [ballot["victory"]["votedFor"] for ballot in ballots]
            checks[f"{civilization}_two_native_ballots"] = (
                len(ballots) == 2 and ballots[0]["turn"] == 0 and ballots[1]["turn"] > 1
            )
            checks[f"{civilization}_guided_choices"] = choices == (
                [None, None]
                if civilization == "Greece"
                else ["Greece", "Greece"]
                if civilization == "Rome"
                else ["Rome", "Greece"]
            )
            acknowledgements = [
                row["receipt"]["victory"]["acknowledgedResults"]
                for row in rows
                if row["receipt"]["kind"] == "ACKNOWLEDGE_DIPLOMATIC_RESULTS"
            ]
            checks[f"{civilization}_acknowledged_failed_election"] = (
                len(acknowledgements) == 1
                and acknowledgements[0]["winner"] is None
                and acknowledgements[0]["minimumVotes"] == 3
                and {r["civilizationId"]: r["votesReceived"] for r in acknowledgements[0]["rows"]}
                == {"Rome": 1, "Greece": 2, "France": 0}
            )
            final_votes = results[civilization].victory.votingResults
            checks[f"{civilization}_received_winning_election"] = (
                final_votes is not None
                and final_votes.winner == "Greece"
                and not final_votes.canAcknowledge
                and next(r for r in final_votes.rows if r.civilizationId == "Greece").votesReceived == 3
            )
            milestones.extend(
                {"civilization": civilization, "turn": b["turn"], "choice": b["victory"]} for b in ballots
            )
        checks["later_election_processed_natively"] = game["diplomaticVictoryVotesProcessed"] is True and game[
            "diplomaticVictoryVotesCast"
        ] == {"Rome": "Greece", "Greece": None, "France": "Greece"}
    return {
        "scenario": kind,
        "passed": all(checks.values()),
        "checks": checks,
        "modelInvocations": len(settings),
        "gameplayActivations": len(activations),
        "victory": game.get("victoryData"),
        "milestones": milestones,
        "limits": [
            "Prepared late-game Vanilla position",
            "Guided player objectives",
            "No Ancient-to-victory autonomous match",
            "No live crash injection",
        ],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-dir", type=Path, required=True)
    args = parser.parse_args()
    report = verify(args.state_dir)
    (args.state_dir / "victory-live-verification.json").write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report, indent=2))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
