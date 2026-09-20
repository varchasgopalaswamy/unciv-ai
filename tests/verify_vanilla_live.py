"""Reconcile a stopped ordinary live match's exported metrics with durable host/service records."""

import argparse
import json
import sqlite3
from pathlib import Path

from civ_agent.telemetry import TOKEN_KEYS


def verify(root: Path, require_finished: bool = False) -> dict:
    checkpoint = json.loads((root / "exploration.json").read_text())
    game = json.loads(checkpoint["gameJson"])
    state = checkpoint["communication"]
    telemetry = root / "telemetry"
    result = json.loads((telemetry / "game-result.json").read_text())
    jobs = {row["jobId"]: row["job"] for row in map(json.loads, (telemetry / "jobs.jsonl").read_text().splitlines())}
    turns = list(map(json.loads, (telemetry / "turns.jsonl").read_text().splitlines()))
    assert jobs == state["jobs"], "Exported jobs differ from the committed host records"
    assert result["matchId"] == game["gameId"]
    assert len(turns) == len({row["turnId"] for row in turns}), "Duplicate turn summaries"
    nations = {civ["civID"]: civ for civ in game["civilizations"]}
    assert all(nations[civ]["playerType"] == "Human" for civ in ("Rome", "Greece", "France"))
    assert nations["Egypt"].get("playerType", "AI") == "AI"
    assert game["gameParameters"]["numberOfCityStates"] == 2
    assert {row["civilization"] for row in result["scores"]} == {"Rome", "Greece", "France", "Egypt"}
    assert all(job["civilization"] != "Egypt" for job in jobs.values()), "A model controlled the built-in AI"

    def assert_usage(actual, selected):
        expected = {key: sum((job.get("usage") or {}).get(key, 0) for job in selected) for key in TOKEN_KEYS}
        assert {key: actual[key] for key in TOKEN_KEYS} == expected
        assert actual["total_tokens"] == expected["input_tokens"] + expected["output_tokens"]

    assert_usage(result["usage"], list(jobs.values()))
    for row in turns:
        turn = row["turn"]
        selected = [jobs[id] for id in row["jobs"]]
        assert all(
            job["purpose"] == "gameplay"
            and job["civilization"] == turn["civilization"]
            and job["epoch"] == turn["epoch"]
            and job["handoff"] == turn["handoff"]
            for job in selected
        )
        assert_usage(row["usage"], selected)
        if turn["endedAtMs"] is not None:
            assert row["elapsedMs"] == max(0, turn["endedAtMs"] - turn["startedAtMs"])
        assert turn["activeMs"] >= 0
        if row["accountingComplete"]:
            assert selected and all(job["telemetry"]["usageComplete"] for job in selected)
    services = 0
    for path in (root / "agent-services").glob("*/telemetry/accounting.sqlite3"):
        services += 1
        db = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            for id, payload in db.execute("SELECT id,payload FROM jobs"):
                local = json.loads(payload)
                if not local["claimed"]:
                    continue
                assert id in jobs, "Service invocation absent from the host"
                assert jobs[id]["usage"] == local["usage"], f"Unreported spending for {id}"
                assert jobs[id]["telemetry"]["apiCalls"] == local["telemetry"]["apiCalls"], (
                    f"Unreported tool calls for {id}"
                )
                settings = local["telemetry"]
                if settings["model"] is not None:
                    assert (settings["model"], settings["reasoningEffort"]) == ("gpt-5.6-luna", "medium")
        finally:
            db.close()
    assert services == 3
    victory = game.get("victoryData")
    if require_finished:
        assert victory and result["status"] == "completed", "The native game has not finished"
    if result["status"] == "completed":
        assert victory, "An unfinished game was reported as won"
        assert result["result"]["winner"] == victory["winningCiv"]
        assert result["result"]["victoryType"] == victory["victoryType"]
    else:
        assert result["result"] is None
    return {
        "gameTurn": game.get("turns", 0),
        "handoffs": checkpoint["handoff"],
        "status": result["status"],
        "turnRecords": len(turns),
        "modelJobs": len(jobs),
        "usage": result["usage"],
        "incompleteJobs": sum(not (job.get("telemetry") or {}).get("usageComplete", False) for job in jobs.values()),
        "winner": (result.get("result") or {}).get("winner"),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("state_dir", type=Path)
    parser.add_argument("--require-finished", action="store_true")
    args = parser.parse_args()
    print(json.dumps(verify(args.state_dir, args.require_finished), indent=2))


if __name__ == "__main__":
    main()
