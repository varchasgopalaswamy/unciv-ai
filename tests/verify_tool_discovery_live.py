"""Measure discovery from provider traces without copying raw reasoning or credentials."""

import argparse
import contextlib
import json
import sqlite3
from pathlib import Path

from civ_agent.telemetry import TOKEN_KEYS
from civ_agent.tls import write_private_json


def discovery_metrics(path: Path):
    requests = {}
    lookups = {}
    settings = []
    for line in path.read_text().splitlines():
        event = json.loads(line)
        item = event.get("payload", {})
        if event["type"] == "turn_context":
            settings.append({"model": item.get("model"), "reasoningEffort": item.get("effort")})
        if event["type"] == "token_usage_record":
            # These are per-response records; token_count notifications are cumulative.
            requests[item["response_id"]] = item["usage"]
        if event["type"] != "response_item":
            continue
        if item.get("type") == "custom_tool_call" and "ALL_TOOLS" in item.get("input", ""):
            lookups[item["call_id"]] = {
                "request": len(requests) + 1,
                "code": item["input"],
                "indexChars": 0,
                "definitionChars": 0,
                "otherChars": 0,
                "index": [],
                "definitions": [],
                "truncated": False,
            }
        if item.get("type") != "custom_tool_call_output" or item["call_id"] not in lookups:
            continue
        lookup = lookups[item["call_id"]]
        for block in item.get("output", []):
            text = block.get("text", "")
            if text.startswith("Script completed\n"):
                continue
            lookup["truncated"] |= "truncated output" in text
            try:
                value = json.loads(text)
            except ValueError:
                lookup["otherChars"] += len(text)
                continue
            entries = value if isinstance(value, list) else [value]
            if entries and all(isinstance(entry, str) for entry in entries):
                lookup["indexChars"] += len(text)
                lookup["index"].extend(entries)
            elif entries and all(isinstance(entry, dict) and "name" in entry for entry in entries):
                field = "definitions" if any("description" in entry for entry in entries) else "index"
                lookup["definitionChars" if field == "definitions" else "indexChars"] += len(text)
                lookup[field].extend(entry["name"] for entry in entries)
            else:
                lookup["otherChars"] += len(text)
    assert requests, "Provider per-request usage records are required for this comparison"
    usage = {key: sum(row[key] for row in requests.values()) for key in TOKEN_KEYS}
    usage["uncached_input_tokens"] = usage["input_tokens"] - usage["cached_input_tokens"]
    seen = set()
    repeats = []
    for lookup in lookups.values():
        repeats.extend(name for name in lookup["definitions"] if name in seen)
        seen.update(lookup["definitions"])
    return {
        "modelSettings": settings,
        "modelRequests": len(requests),
        "usage": usage,
        "discoveryRequests": len({row["request"] for row in lookups.values()}),
        "discoveryChars": sum(
            row[k] for row in lookups.values() for k in ("indexChars", "definitionChars", "otherChars")
        ),
        "indexChars": sum(row["indexChars"] for row in lookups.values()),
        "definitionChars": sum(row["definitionChars"] for row in lookups.values()),
        "repeatedDefinitions": repeats,
        "truncatedDiscoveryOutputs": sum(row["truncated"] for row in lookups.values()),
        "maxDefinitionsPerLookup": max((len(row["definitions"]) for row in lookups.values()), default=0),
        "lookups": list(lookups.values()),
    }


def verify(root: Path, sessions: Path):
    record = json.loads((root / "discovery-run.json").read_text())
    assert record["phase"] == "COMPLETED"
    assert (record["model"], record["reasoningEffort"]) == ("gpt-5.6-luna", "medium")
    paths = list(sessions.rglob(f"*{record['threadId']}.jsonl"))
    assert len(paths) == 1, "Expected exactly one provider trace for the recorded thread"
    metrics = discovery_metrics(paths[0])
    assert metrics["modelSettings"] and all(
        settings == {"model": "gpt-5.6-luna", "reasoningEffort": "medium"} for settings in metrics["modelSettings"]
    ), "Provider settings differ from the comparison configuration"
    assert {key: metrics["usage"][key] for key in TOKEN_KEYS} == record["usage"]
    jobs = {
        row["jobId"]: row["job"] for row in map(json.loads, (root / "telemetry/jobs.jsonl").read_text().splitlines())
    }
    assert jobs[record["activationId"]]["usage"] == record["usage"]
    assert jobs[record["activationId"]]["telemetry"]["usageComplete"]
    with contextlib.closing(
        sqlite3.connect(f"file:{root / 'agent-services/Rome/civilization/memory.sqlite3'}?mode=ro", uri=True)
    ) as db:
        calls = list(db.execute("SELECT name,arguments,result FROM calls WHERE job=?", (record["activationId"],)))
    metrics["toolCalls"] = len(calls)
    metrics["errors"] = [
        {"tool": name, "error": json.loads(result)["error"]}
        for name, _, result in calls
        if result and "error" in json.loads(result)
    ]
    metrics["turnOverviewCalls"] = sum(name == "player_turn" for name, _, _ in calls)
    metrics["toolsUsed"] = sorted({name for name, _, _ in calls})
    if record["purpose"] == "communication":
        assert any(name == "chat_read" for name, _, _ in calls)
        assert any(name == "chat_send" and result and "error" not in json.loads(result) for name, _, result in calls), (
            "The communication job did not send a successful reply"
        )
    report = {**record, **metrics}
    write_private_json(root / "discovery-metrics.json", report)
    return {
        key: value for key, value in report.items() if key not in ("lookups", "agentSource", "threadId", "activationId")
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("state_dir", type=Path)
    parser.add_argument("--sessions", type=Path, default=Path.home() / ".codex/sessions")
    args = parser.parse_args()
    print(json.dumps(verify(args.state_dir, args.sessions), indent=2))


if __name__ == "__main__":
    main()
