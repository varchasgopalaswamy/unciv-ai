"""Compare stopped opening batches using provider usage and durable game/service records."""

import argparse
import contextlib
import hashlib
import json
import sqlite3
from collections import Counter
from pathlib import Path

from civ_agent.protocol import JsonObject
from civ_agent.runner_history import Chunks
from civ_agent.telemetry import TOKEN_KEYS
from civ_agent.tls import write_private_json
from tests.verify_state_updates_live import measure as measure_updates
from tests.verify_tool_discovery_live import discovery_metrics
from tests.verify_tool_results_live import encoded, result_metrics
from tests.verify_vanilla_live import verify


def sum_usage(jobs):
    usage = {key: sum(job["usage"][key] for job in jobs) for key in TOKEN_KEYS}
    usage["uncached_input_tokens"] = usage["input_tokens"] - usage["cached_input_tokens"]
    return usage


def summarize(jobs):
    fields = (
        "modelRequests",
        "toolCalls",
        "toolErrors",
        "successfulCommands",
        "overviewCalls",
        "jobElapsedMs",
        "toolHandlingMs",
        "negotiationWaitMs",
        "rejectedMemoryChanges",
        "startupCalls",
        "startupChars",
    )
    result: JsonObject = {key: sum(job[key] for job in jobs) for key in fields}
    result.update(jobs=len(jobs), usage=sum_usage(jobs))
    for key in ("firstRequestUsage", "laterRequestUsage"):
        result[key] = {name: sum(job[key][name] for job in jobs) for name in (*TOKEN_KEYS, "uncached_input_tokens")}
        result[key]["cache_write_input_tokens"] = (
            sum(job[key]["cache_write_input_tokens"] for job in jobs)
            if all(job[key]["cache_write_input_tokens"] is not None for job in jobs)
            else None
        )
    for key in ("gameplayElapsedMs", "finalizationElapsedMs"):
        result[key] = sum(job[key] for job in jobs) if jobs and all(job.get(key) is not None for job in jobs) else None
    result["responseMetrics"] = {
        key: sum(job["responses"].get(key, 0) for job in jobs)
        for key in sorted({key for job in jobs for key in job["responses"]})
    }
    result["stateUpdates"] = dict(
        sum((Counter(job.get("stateUpdates", {}).get("updates", {})) for job in jobs), Counter())
    )
    for key in ("reconstructedSnapshots", "deltaChars", "equivalentFullUpdateChars", "invalidatedInspections"):
        result[key] = sum(job.get("stateUpdates", {}).get(key, 0) for job in jobs)
    result["toolCallBreakdown"] = dict(sum((Counter(job["tools"]) for job in jobs), Counter()))
    result["successfulCommandBreakdown"] = dict(sum((Counter(job["commands"]) for job in jobs), Counter()))
    return result


def collect(root, sessions, require_updates=False):
    verified = verify(root)
    assert verified["incompleteJobs"] == 0
    jobs = {
        row["jobId"]: row["job"] for row in map(json.loads, (root / "telemetry/jobs.jsonl").read_text().splitlines())
    }
    turns = list(map(json.loads, (root / "telemetry/turns.jsonl").read_text().splitlines()))
    assert all(row["accountingComplete"] and row["turn"]["status"] == "completed" for row in turns)
    activations = {}
    for pattern in ("*/activations/*.json", "*/chat-jobs/*.json"):
        for path in (root / "agent-services").glob(pattern):
            value = json.loads(path.read_text())
            activations[value["activationId"]] = value
    rows, threads = [], set()
    for id, job in sorted(jobs.items(), key=lambda pair: pair[1]["telemetry"]["startedAtMs"]):
        activation = activations[id]
        assert activation["phase"] == "COMPLETED"
        thread = activation["threadId"]
        assert thread not in threads, "This comparison requires one fresh model conversation per job"
        threads.add(thread)
        paths = list(sessions.rglob(f"*{thread}.jsonl"))
        assert len(paths) == 1, f"Missing/ambiguous provider trace for {id}"
        discovery = discovery_metrics(paths[0])
        assert discovery["modelSettings"] and all(
            setting == activation["effectiveSettings"] for setting in discovery["modelSettings"]
        )
        assert {key: discovery["usage"][key] for key in TOKEN_KEYS} == job["usage"], f"Provider usage differs for {id}"
        service = root / "agent-services" / job["civilization"]
        with contextlib.closing(
            sqlite3.connect(f"file:{service / 'civilization/memory.sqlite3'}?mode=ro", uri=True)
        ) as db:
            all_calls = [
                (call_id, name, json.loads(arguments), json.loads(result) if result else None)
                for call_id, name, arguments, result in db.execute(
                    "SELECT id,name,arguments,result FROM calls WHERE job=? ORDER BY rowid", (id,)
                )
            ]
            calls = [
                (name, arguments, result)
                for call_id, name, arguments, result in all_calls
                if not call_id.startswith("startup:")
            ]
            prepared_calls = [
                (name, arguments, result)
                for call_id, name, arguments, result in all_calls
                if call_id.startswith("startup:")
            ]
            snapshots = [
                json.loads(body) for (body,) in db.execute("SELECT body FROM records WHERE key='observation:game'")
            ]
        chunks, audited, withheld = Chunks(), [], 0
        history = service / "history" / hashlib.sha256(id.encode()).hexdigest() / "events.jsonl"
        for line in history.read_text().splitlines():
            event = chunks.expand(json.loads(line))
            if event is not None and event["kind"] == "tool_result":
                result = event["data"].get("result")
                if isinstance(result, dict) and isinstance(result.get("structuredContent"), dict):
                    audited.append(result["structuredContent"])
                    withheld += event["data"].get("deliveredToModel") is False
        durable = Counter(encoded(result) for _, _, result in calls if result is not None)
        audit = Counter(encoded(result) for result in audited)
        assert not durable - audit, f"Durable response missing from audit for {id}"
        responses = result_metrics(paths[0], audited)
        responses["withheldHandoffResults"] = withheld
        assert responses["unmatchedPrintedResults"] == 0
        telemetry = job["telemetry"]
        tool_calls = {
            key.removeprefix("tool:"): value for key, value in telemetry["apiCalls"].items() if key.startswith("tool:")
        }
        row = {
            "activationId": id,
            "civilization": job["civilization"],
            "purpose": job["purpose"],
            "gameTurn": job.get("gameTurn"),
            "usage": discovery["usage"],
            "firstRequestUsage": discovery["firstRequestUsage"],
            "laterRequestUsage": discovery["laterRequestUsage"],
            "promptLayout": activation.get("startup"),
            "modelRequests": discovery["modelRequests"],
            "discoveryChars": discovery["discoveryChars"],
            "toolCalls": sum(value["calls"] for value in tool_calls.values()),
            "toolErrors": sum(value["errors"] for value in tool_calls.values()),
            "successfulCommands": tool_calls.get("player_command", {}).get("succeeded", 0),
            "overviewCalls": tool_calls.get("player_turn", {}).get("calls", 0),
            "jobElapsedMs": telemetry["elapsedMs"],
            "gameplayElapsedMs": telemetry.get("gameplayElapsedMs"),
            "finalizationElapsedMs": telemetry.get("finalizationElapsedMs"),
            "startupCalls": sum(
                value["calls"] for name, value in telemetry["apiCalls"].items() if name.startswith("startup:")
            ),
            "startupChars": activation.get("startup", {}).get("characters", 0),
            "toolHandlingMs": sum(value["elapsedMs"] for value in tool_calls.values()),
            "negotiationWaitMs": telemetry["negotiationWaitMs"],
            "tools": {name: value["calls"] for name, value in tool_calls.items()},
            "commands": {
                key.removeprefix("command:"): value["succeeded"]
                for key, value in telemetry["apiCalls"].items()
                if key.startswith("command:")
            },
            "errors": [
                {"tool": name, "error": result["error"]}
                for name, _, result in calls
                if result is not None and "error" in result
            ],
            "rejectedMemoryChanges": sum(
                change.get("accepted") is False
                for name, _, result in calls
                if name == "memory_update" and result is not None
                for change in result.get("changes", [])
            ),
            "responses": responses,
        }
        assert Counter(name for name, _, _ in calls) == row["tools"], f"Call counts differ for {id}"
        assert Counter(name for name, _, _ in prepared_calls) == {
            key.removeprefix("startup:"): value["calls"]
            for key, value in telemetry["apiCalls"].items()
            if key.startswith("startup:")
        }, f"Startup counts differ for {id}"
        if require_updates:
            assert responses["duplicateTextBlocks"] == 0
            if job["purpose"] == "gameplay":
                row["stateUpdates"] = measure_updates(
                    [(name, args, result) for _, name, args, result in all_calls], snapshots, id
                )
                assert row["stateUpdates"]["updates"].get("turn_ended") == 1
        rows.append(row)
    report = {
        "root": str(root),
        "verification": verified,
        "allJobs": summarize(rows),
        "gameplay": summarize([job for job in rows if job["purpose"] == "gameplay"]),
        "communication": summarize([job for job in rows if job["purpose"] == "communication"]),
        "gameplayWallMs": max(job["telemetry"]["endedAtMs"] for job in jobs.values() if job["purpose"] == "gameplay")
        - min(job["telemetry"]["startedAtMs"] for job in jobs.values() if job["purpose"] == "gameplay"),
        "hostOverviewCalls": sum(turn["hostCalls"].get("tool:player_turn", {}).get("calls", 0) for turn in turns),
        "hostGameHandlingMs": sum(
            value["elapsedMs"]
            for turn in turns
            for key, value in turn["hostCalls"].items()
            if key.startswith("tool:") and key != "tool:player_communication"
        ),
        "jobs": rows,
    }
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("baseline", type=Path)
    parser.add_argument("current", type=Path)
    parser.add_argument("--sessions", type=Path, default=Path.home() / ".codex/sessions")
    args = parser.parse_args()
    report = {"baseline": collect(args.baseline, args.sessions), "current": collect(args.current, args.sessions, True)}
    for key in ("gameTurn", "handoffs", "turnRecords", "modelSettings"):
        assert report["baseline"]["verification"][key] == report["current"]["verification"][key]
    record = json.loads((args.current / "live-run.json").read_text())
    checkpoint = json.loads(Path(record["sourceCheckpoint"]).read_text())
    checkpoint = checkpoint.get("host", checkpoint)
    assert record["checkpointSha256"] == hashlib.sha256(json.dumps(checkpoint, sort_keys=True).encode()).hexdigest()
    assert Path(record["sourceState"]).resolve() == args.baseline.resolve()
    report["checkpointSha256"] = record["checkpointSha256"]
    report["run"] = record
    write_private_json(args.current / "opening-comparison.json", report)
    print(
        json.dumps(
            {
                label: {
                    key: report[label][key]
                    for key in ("verification", "allJobs", "gameplayWallMs", "hostOverviewCalls", "hostGameHandlingMs")
                }
                for label in ("baseline", "current")
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
