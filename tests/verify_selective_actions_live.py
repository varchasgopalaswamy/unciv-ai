"""Measure focused reads and offered commands without exporting game text or reasoning."""

import argparse
import contextlib
import json
import sqlite3
from collections import Counter
from pathlib import Path

from civ_agent.observations import DetailQuery, project_detail
from civ_agent.proposal_gateway import public_result
from civ_agent.tls import write_private_json
from tests.verify_tool_results_live import encoded
from tests.verify_tool_results_live import verify as verify_results

TEMPLATE_FIELDS = {
    "command",
    "commands",
    "foundCityCommand",
    "foundCityConfirmationField",
    "stopCommand",
    "removeCommand",
    "moveCommands",
    "confirmationField",
    "inspection",
}


def without_templates(value):
    if isinstance(value, dict):
        return {key: without_templates(part) for key, part in value.items() if key not in TEMPLATE_FIELDS}
    if isinstance(value, list):
        return [without_templates(part) for part in value]
    return value


def offered_commands(value):
    if isinstance(value, dict):
        for key, part in value.items():
            if key in TEMPLATE_FIELDS:
                for command in part if isinstance(part, list) else [part]:
                    if isinstance(command, dict) and "kind" in command:
                        yield command
            else:
                yield from offered_commands(part)
    elif isinstance(value, list):
        for part in value:
            yield from offered_commands(part)


def measure(calls, snapshots):
    focused, named, matches, templates = [], Counter(), Counter(), []
    added_chars = 0
    for name, arguments, result in calls:
        if not result:
            continue
        if name == "player_command":
            choices = {
                k: v for k, v in arguments.items() if k not in {"intent", "confirmAction", "confirmBreakPromise"}
            }
            if choices in templates:
                matches[choices["kind"]] += 1
            continue
        if "error" in result:
            continue
        offered = list(offered_commands(result))
        if offered:
            templates.extend(offered)
            added_chars += len(encoded(result)) - len(encoded(without_templates(result)))
        if name in {"research_details", "construction_details", "policy_details"} and arguments.get("names"):
            named[name] += 1
        if name not in {"unit_details", "city_details"} or "all" in arguments.get("sections", ["all"]):
            continue
        query = DetailQuery.model_validate(result["query"])
        # Match the delivered projection to its permitted persisted snapshot; no hidden state is consulted.
        snapshot = next((s for s in snapshots if public_result(project_detail(s, query)) == result), None)
        assert snapshot is not None, "Cannot match focused read to its persisted permitted snapshot"
        full_query = DetailQuery(tool=name, arguments={"target": arguments["target"], "sections": ["all"]})
        full = public_result(project_detail(snapshot, full_query))
        focused.append(
            {
                "tool": name,
                "sections": query.arguments["sections"],
                "focusedChars": len(encoded(result)),
                "equivalentFullChars": len(encoded(full)),
            }
        )
    return {
        "focusedReads": focused,
        "namedReads": dict(named),
        "commandsMatchingDeliveredOffers": dict(matches),
        "commandTemplateAddedChars": added_chars,
        "focusedChars": sum(row["focusedChars"] for row in focused),
        "equivalentFullChars": sum(row["equivalentFullChars"] for row in focused),
    }


def verify(root, sessions):
    report = verify_results(root, sessions)
    record = json.loads((root / "discovery-run.json").read_text())
    with contextlib.closing(
        sqlite3.connect(f"file:{root / 'agent-services/Rome/civilization/memory.sqlite3'}?mode=ro", uri=True)
    ) as db:
        calls = [
            (name, json.loads(args), json.loads(result) if result else None)
            for name, args, result in db.execute(
                "SELECT name,arguments,result FROM calls WHERE job=? ORDER BY rowid", (record["activationId"],)
            )
        ]
        snapshots = [
            json.loads(body) for (body,) in db.execute("SELECT body FROM records WHERE key='observation:game'")
        ]
    metrics = measure(calls, snapshots)
    telemetry = next(
        row["job"]["telemetry"]
        for row in map(json.loads, (root / "telemetry/jobs.jsonl").read_text().splitlines())
        if row["jobId"] == record["activationId"]
    )
    counts = Counter(name for name, _, _ in calls)
    assert dict(counts) == {
        key.removeprefix("tool:"): entry["calls"]
        for key, entry in telemetry["apiCalls"].items()
        if key.startswith("tool:")
    }
    metrics["elapsedMs"] = telemetry["elapsedMs"]
    metrics["apiCalls"] = telemetry["apiCalls"]
    accounting = root / "agent-services/Rome/telemetry/accounting.sqlite3"
    with contextlib.closing(sqlite3.connect(f"file:{accounting}?mode=ro", uri=True)) as db:
        local = json.loads(db.execute("SELECT payload FROM jobs WHERE id=?", (record["activationId"],)).fetchone()[0])
    assert local["usage"] == record["usage"]
    assert local["telemetry"]["apiCalls"] == telemetry["apiCalls"]
    windows = list(map(json.loads, (root / "telemetry/host-api.jsonl").read_text().splitlines()))
    metrics["hostApiWindows"] = [
        {"handoff": row["handoff"], "gameTurn": row["gameTurn"], "apiCalls": row["actors"]["Rome"]}
        for row in windows
        if "Rome" in row["actors"]
    ]
    metrics["hostToolCalls"] = sum(
        entry["calls"]
        for window in metrics["hostApiWindows"]
        for key, entry in window["apiCalls"].items()
        if key.startswith("tool:")
    )
    if record["purpose"] == "gameplay":
        assert metrics["focusedReads"] and metrics["namedReads"].get("research_details")
        assert metrics["namedReads"].get("construction_details")
        assert metrics["commandsMatchingDeliveredOffers"].get("SET_RESEARCH_QUEUE")
        assert metrics["commandsMatchingDeliveredOffers"].get("QUEUE_CONSTRUCTION")
        turn = next(
            row
            for row in map(json.loads, (root / "telemetry/turns.jsonl").read_text().splitlines())
            if record["activationId"] in row["jobs"]
        )
        assert turn["accountingComplete"] and turn["turn"]["status"] == "completed"
        assert turn["agentCalls"] == telemetry["apiCalls"]
        window = next(
            row
            for row in windows
            if row["epoch"] == turn["turn"]["epoch"] and row["handoff"] == turn["turn"]["handoff"]
        )
        assert turn["hostCalls"] == window["actors"]["Rome"]
    report["selectiveActions"] = metrics
    write_private_json(root / "selective-action-metrics.json", report)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("state_dir", type=Path)
    parser.add_argument("--sessions", type=Path, default=Path.home() / ".codex/sessions")
    args = parser.parse_args()
    report = verify(args.state_dir, args.sessions)
    print(
        json.dumps(
            {
                key: value
                for key, value in report["selectiveActions"].items()
                if key not in {"apiCalls", "hostApiWindows"}
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
