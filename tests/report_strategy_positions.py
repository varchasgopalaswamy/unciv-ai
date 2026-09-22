"""Read-only metrics and decision evidence for a live match or saved-position run."""

import argparse
import json
from collections import Counter
from pathlib import Path

from sqlalchemy import URL, create_engine, select

from civ_agent import memory_schema as tables
from civ_agent.protocol import JsonObject
from civ_agent.telemetry import TOKEN_KEYS
from tests.verify_tool_discovery_live import discovery_metrics


def json_lines(path):
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


def native_position(checkpoint_path, civilizations):
    """Operator-only final facts, including consequences after the last handoff."""
    game = json.loads(json.loads(checkpoint_path.read_text())["gameJson"])
    positions = []
    for name in sorted(civilizations):
        civ = next(c for c in game["civilizations"] if c["civID"] == name)
        units = []
        for tile in game["tileMap"]["tileList"]:
            for unit in [tile.get("militaryUnit"), tile.get("civilianUnit"), *tile.get("airUnits", [])]:
                if unit is not None and unit.get("owner") == name:
                    position = tile.get("position", {})
                    units.append(
                        {
                            "id": unit["id"],
                            "name": unit["name"],
                            "health": unit.get("health", 100),
                            "position": {"x": position.get("x", 0), "y": position.get("y", 0)},
                            "action": unit.get("action"),
                            "improvement": tile.get("improvement"),
                            "improvementQueue": tile.get("improvementQueue", []),
                        }
                    )
        positions.append(
            {
                "civilization": name,
                "turn": game.get("turns", 0),
                "exploredTiles": sum(name in tile.get("exploredBy", []) for tile in game["tileMap"]["tileList"]),
                "units": units,
                "cities": [
                    {
                        "name": c["name"],
                        "population": c.get("population", {}).get("population", 1),
                        "queue": c.get("cityConstructions", {}).get("constructionQueue", []),
                        "position": c["location"],
                    }
                    for c in civ.get("cities", [])
                ],
                "researchQueue": civ["tech"].get("techsToResearch", []),
            }
        )
    return positions


def analyze(root, sessions, rounds=None):
    latest = {row["turnId"]: row for row in json_lines(root / "telemetry/turns.jsonl")}
    turns = [row for row in latest.values() if row["turn"]["status"] == "completed"]
    turns.sort(key=lambda row: row["turn"]["handoff"])
    if rounds and turns:
        first = min(row["turn"]["gameTurn"] for row in turns)
        turns = [row for row in turns if row["turn"]["gameTurn"] < first + rounds]
    assert turns and all(row["accountingComplete"] for row in turns), "Incomplete turn accounting"
    job_ids = {job for row in turns for job in row["jobs"]}
    jobs = {row["jobId"]: row["job"] for row in json_lines(root / "telemetry/jobs.jsonl")}
    traces = {path.name[-42:-6]: path for path in sessions.rglob("rollout-*.jsonl")}
    activations, missing_traces = [], []
    for path in root.glob("agent-services/*/activations/*.json"):
        record = json.loads(path.read_text())
        if record["activationId"] not in job_ids:
            continue
        trace = traces.get(record.get("threadId"))
        metrics: JsonObject | None = discovery_metrics(trace) if trace else None
        job = jobs[record["activationId"]]
        assert job["usage"] == record["usage"], "Host/agent token accounting differs"
        if metrics:
            assert all(metrics["usage"][key] == record["usage"][key] for key in TOKEN_KEYS), (
                "Provider/agent usage differs"
            )
        else:
            missing_traces.append(record.get("threadId"))
        activations.append(
            {
                "id": record["activationId"],
                "civilization": record["civilization"],
                "turn": record["gameTurn"],
                "phase": record["phase"],
                "settings": record.get("effectiveSettings", record.get("settings")),
                "serviceTier": record.get("serviceTier"),
                "startup": record.get("startup"),
                "modelRequests": metrics["modelRequests"] if metrics else None,
                "firstRequestUsage": metrics["firstRequestUsage"] if metrics else None,
                "usage": record["usage"],
            }
        )
    assert {a["id"] for a in activations} == job_ids, "Missing activation records"
    for row in turns:
        assert all(row["usage"][key] == sum(jobs[job]["usage"][key] for job in row["jobs"]) for key in TOKEN_KEYS)
    commands, observations, errors = [], [], []
    calls = Counter()
    for path in root.glob("agent-services/*/civilization/memory.sqlite3"):
        engine = create_engine(
            URL.create("sqlite+pysqlite", database=f"file:{path.resolve()}", query={"mode": "ro", "uri": "true"})
        )
        try:
            with engine.connect() as db:
                rows = db.execute(
                    select(tables.calls).where(tables.calls.c.job.in_(job_ids)).order_by(tables.call_order)
                ).mappings()
                for row in rows:
                    result = json.loads(row["result"]) if row["result"] else {}
                    arguments = json.loads(row["arguments"])
                    calls[("startup:" if row["id"].startswith("startup:") else "tool:") + row["name"]] += 1
                    if "error" in result:
                        errors.append(
                            {"job": row["job"], "tool": row["name"], "arguments": arguments, "error": result["error"]}
                        )
                    if row["name"] == "player_command" or (
                        row["name"] == "chat_send" and arguments.get("nativeAction")
                    ):
                        commands.append(
                            {
                                "job": row["job"],
                                "tool": row["name"],
                                "arguments": arguments,
                                "result": {
                                    k: v
                                    for k, v in result.items()
                                    if k not in {"stateUpdate", "memoryUpdates", "memoryPhase"}
                                },
                            }
                        )
                # Observations are service-owned, so records.job is null. Use
                # the frozen handoff snapshot, not the current observation head.
                rows = db.execute(select(tables.jobs.c.id, tables.jobs.c.snapshot).where(tables.jobs.c.id.in_(job_ids)))
                by_job = {}
                for job, saved in rows:
                    record = json.loads(saved).get("closingRecords", {}).get("observation:game")
                    if record:
                        by_job[job] = record["body"]
                for job, snapshot in by_job.items():
                    observations.append(
                        {
                            "job": job,
                            "turn": snapshot["turn"],
                            "civilization": jobs[job]["civilization"],
                            "exploredTiles": len(snapshot["map"]["tiles"]),
                            "units": [
                                {
                                    k: unit.get(k)
                                    for k in ("handle", "name", "position", "health", "worker", "movementOrder")
                                }
                                for unit in snapshot["map"]["units"]
                            ],
                            "cities": [
                                {
                                    "name": city["name"],
                                    "queue": city["constructionQueue"],
                                    "development": city.get("development"),
                                }
                                for city in snapshot["cities"]
                            ],
                            "researchQueue": snapshot["researchQueue"],
                            "economy": snapshot.get("economy"),
                        }
                    )
        finally:
            engine.dispose()
    usage = {key: sum(row["usage"][key] for row in turns) for key in TOKEN_KEYS}
    usage["uncached_input_tokens"] = usage["input_tokens"] - usage["cached_input_tokens"]
    return {
        "stateDirectory": str(root.resolve()),
        "completedTurns": len(turns),
        "usage": usage,
        "modelRequests": sum(a["modelRequests"] for a in activations) if not missing_traces else None,
        "missingProviderTraces": missing_traces,
        "providerUsageReconciled": not missing_traces,
        "gameplaySeconds": sum(row["gameplayElapsedMs"] for row in turns) / 1000,
        "finalizationSeconds": sum(row["finalizationElapsedMs"] for row in turns) / 1000,
        "toolCalls": dict(calls),
        "errors": errors,
        "memoryEdits": {
            key: sum(row["memoryEdits"][key] for row in turns) for key in ("batches", "accepted", "rejected")
        },
        "activations": sorted(activations, key=lambda row: (row["turn"], row["civilization"])),
        "commands": commands,
        "lastObservedStates": observations,
        "finalNativePositions": native_position(
            root / "exploration.json", {row["turn"]["civilization"] for row in turns}
        )
        if rounds is None
        else None,
        "runner": json.loads((root / "runner/run.json").read_text()).get("status"),
        "limitation": "Final native positions include opponent actions after the last handoff. Omitted for historical round-limited reports whose final checkpoint is from a later turn.",
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("state_dir", type=Path)
    parser.add_argument("--sessions", type=Path, default=Path.home() / ".codex/sessions")
    parser.add_argument("--rounds", type=int)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    report = analyze(args.state_dir, args.sessions, args.rounds)
    args.output.write_text(json.dumps(report, indent=2) + "\n")
    print(
        json.dumps(
            {
                k: v
                for k, v in report.items()
                if k not in {"activations", "commands", "lastObservedStates", "finalNativePositions", "toolCalls"}
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
