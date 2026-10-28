"""Read-only metrics and decision evidence for a live match or saved-position run."""

import argparse
import json
import os
from collections import Counter
from pathlib import Path

from sqlalchemy import URL, create_engine, select

from civ_agent import memory_schema as tables
from civ_agent.memory_snapshots import record_bodies
from civ_agent.monitoring import path_for, read_collection, read_record
from civ_agent.protocol import JsonObject
from civ_agent.review_context import review_position
from civ_agent.telemetry import TOKEN_KEYS
from tests.verify_tool_discovery_live import discovery_metrics


def json_lines(path):
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


def native_position(checkpoint_path, civilizations):
    """Operator-only facts from a checkpoint or raw save, never a player input."""
    saved = json.loads(checkpoint_path.read_text())
    game = json.loads(saved["gameJson"]) if "gameJson" in saved else saved
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
                "gold": civ.get("gold", 0),
                "exploredTiles": sum(name in tile.get("exploredBy", []) for tile in game["tileMap"]["tileList"]),
                "units": units,
                "cities": [
                    {
                        "id": c["id"],
                        "name": c["name"],
                        "population": c.get("population", {}).get("population", 1),
                        "foodStored": c.get("population", {}).get("foodStored", 0),
                        "buildings": c.get("cityConstructions", {}).get("builtBuildings", []),
                        "queue": c.get("cityConstructions", {}).get("constructionQueue", []),
                        "position": {axis: c.get("location", {}).get(axis, 0) for axis in ("x", "y")},
                    }
                    for c in civ.get("cities", [])
                ],
                "researchQueue": civ["tech"].get("techsToResearch", []),
                "technologies": sorted(civ["tech"].get("techsResearched", [])),
                "policies": sorted(civ.get("policies", {}).get("adoptedPolicies", [])),
            }
        )
    return positions


def position_changes(initial, final):
    """Describe outcomes without labeling a move, purchase or build as correct."""
    results = []
    for before in initial:
        after = next(row for row in final if row["civilization"] == before["civilization"])
        units = {unit["id"]: unit for unit in after["units"]}
        old_ids = {unit["id"] for unit in before["units"]}
        counts_before = Counter(unit["name"] for unit in before["units"])
        counts_after = Counter(unit["name"] for unit in after["units"])
        cities = {city["id"]: city for city in after["cities"]}
        old_cities = {city["id"] for city in before["cities"]}
        results.append(
            {
                "civilization": before["civilization"],
                "fromTurn": before["turn"],
                "toTurn": after["turn"],
                "exploredTilesDelta": after["exploredTiles"] - before["exploredTiles"],
                "goldDelta": after["gold"] - before["gold"],
                "populationDelta": sum(c["population"] for c in after["cities"])
                - sum(c["population"] for c in before["cities"]),
                "technologiesAcquired": sorted(set(after["technologies"]) - set(before["technologies"])),
                "policiesAcquired": sorted(set(after["policies"]) - set(before["policies"])),
                "unitCounts": {
                    name: {"before": counts_before[name], "after": counts_after[name]}
                    for name in sorted(counts_before.keys() | counts_after.keys())
                },
                "startingUnits": [{"before": unit, "after": units.get(unit["id"])} for unit in before["units"]],
                "newUnits": [unit for unit in after["units"] if unit["id"] not in old_ids],
                "startingCities": [{"before": city, "after": cities.get(city["id"])} for city in before["cities"]],
                "newCities": [city for city in after["cities"] if city["id"] not in old_cities],
                "interpretation": "A missing unit may have been destroyed, captured, disbanded, upgraded or consumed by founding/another action. Check receipts and combat reports; roster changes alone are not casualty counts.",
            }
        )
    return results


def analyze(root, sessions, rounds=None):
    latest = {row["turnId"]: row for row in read_collection(path_for(root), "turns")}
    unfinished = sum(row["turn"]["status"] != "completed" or not row["accountingComplete"] for row in latest.values())
    turns = [row for row in latest.values() if row["turn"]["status"] == "completed"]
    turns.sort(key=lambda row: row["turn"]["handoff"])
    if rounds and turns:
        first = min(row["turn"]["gameTurn"] for row in turns)
        turns = [row for row in turns if row["turn"]["gameTurn"] < first + rounds]
    assert turns and all(row["accountingComplete"] for row in turns), "Incomplete turn accounting"
    job_ids = {job for row in turns for job in row["jobs"]}
    jobs = {row["jobId"]: row["job"] for row in read_collection(path_for(root), "jobs")}
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
    choices, feedback, comparisons, reviews = [], [], [], []
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
                    if result.get("decisionFeedback", {}).get("choices"):
                        feedback.append({"job": row["job"], "feedback": result["decisionFeedback"]})
                    if row["name"] == "construction_details" and arguments.get("horizonTurns"):
                        comparisons.append({"job": row["job"], "arguments": arguments, "result": result})
                    calls[("startup:" if row["id"].startswith("startup:") else "tool:") + row["name"]] += 1
                    if "error" in result:
                        errors.append(
                            {"job": row["job"], "tool": row["name"], "arguments": arguments, "error": result["error"]}
                        )
                    if row["name"] == "memory_update":
                        errors.extend(
                            {"job": row["job"], "tool": row["name"], "edit": index, "rejection": change}
                            for index, change in enumerate(result.get("changes", []))
                            if not change.get("accepted")
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
                                    k: v for k, v in result.items() if k not in {"stateUpdate", "memoryUpdates"}
                                },
                            }
                        )
                # Observations are service-owned, so records.job is null. Use
                # the frozen handoff snapshot, not the current observation head.
                rows = db.execute(select(tables.jobs.c.id, tables.jobs.c.snapshot).where(tables.jobs.c.id.in_(job_ids)))
                by_job = {}
                for job, saved in rows:
                    version = json.loads(saved).get("gameViewVersion")
                    if version is not None:
                        ref = ("observation:game", version)
                        by_job[job] = record_bodies(db, [ref])[ref]["body"]
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
                            "outputSummary": review_position(snapshot)["totals"],
                        }
                    )
                for row in db.execute(
                    select(tables.records)
                    .where(tables.records.c.category == "strategy", tables.records.c.job.in_(job_ids))
                    .order_by(tables.record_order)
                ).mappings():
                    body = json.loads(row["body"])
                    choices.append(
                        {
                            "job": row["job"],
                            "strategy": row["key"],
                            "version": row["version"],
                            "turn": body.get("updatedTurn"),
                            "state": body.get("state"),
                            "decisions": body.get("decisions", []),
                        }
                    )
                first_turn = min(row["turn"]["gameTurn"] for row in turns)
                last_turn = max(row["turn"]["gameTurn"] for row in turns)
                for row in db.execute(
                    select(tables.reviews.c.turn, tables.reviews.c.advice)
                    .where(tables.reviews.c.status == "COMPLETED", tables.reviews.c.turn.between(first_turn, last_turn))
                    .order_by(tables.reviews.c.turn)
                ).mappings():
                    reviews.append(
                        {
                            "civilization": path.parent.parent.name,
                            "turn": row["turn"],
                            "advice": json.loads(row["advice"]),
                        }
                    )
        finally:
            engine.dispose()
    usage = {key: sum(row["usage"][key] for row in turns) for key in TOKEN_KEYS}
    usage["uncached_input_tokens"] = usage["input_tokens"] - usage["cached_input_tokens"]
    civilizations = {row["turn"]["civilization"] for row in turns}
    final = native_position(root / "exploration.json", civilizations) if rounds is None and not unfinished else None
    initial_path = root / "initial-game.json"
    initial = native_position(initial_path, civilizations) if initial_path.exists() else None
    return {
        "stateDirectory": str(root.resolve()),
        "completedTurns": len(turns),
        "unfinishedTurns": unfinished,
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
        "decisionEvidence": {
            "choices": choices,
            "feedback": feedback,
            "investmentComparisons": comparisons,
            "reviews": reviews,
        },
        "lastObservedStates": observations,
        "initialNativePositions": initial,
        "finalNativePositions": final,
        "outcomes": position_changes(initial, final) if initial and final else None,
        "runner": (read_record(path_for(root), "state", "run") or {}).get("status"),
        "limitation": "Final native positions include opponent actions after the last handoff. Omitted when unfinished turns/accounting exist or for historical round-limited reports whose final checkpoint is from a later turn.",
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("state_dir", type=Path)
    parser.add_argument(
        "--sessions", type=Path, default=Path(os.environ.get("CODEX_HOME", Path.home() / ".codex")) / "sessions"
    )
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
                if k
                not in {
                    "activations",
                    "commands",
                    "lastObservedStates",
                    "initialNativePositions",
                    "finalNativePositions",
                    "outcomes",
                    "toolCalls",
                    "decisionEvidence",
                }
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
