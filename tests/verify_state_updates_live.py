"""Reconstruct command deltas and compare them with persisted permitted snapshots."""

import argparse
import contextlib
import json
import sqlite3
from collections import Counter
from pathlib import Path

from civ_agent.observations import overview
from civ_agent.proposal_gateway import public_result
from civ_agent.state_updates import apply_overview_update, stamp
from civ_agent.tls import write_private_json
from tests.verify_tool_results_live import encoded
from tests.verify_tool_results_live import verify as verify_results


def measure(calls, snapshots, job_id):
    current = None
    kinds, fields = Counter(), Counter()
    matched, delta_chars, full_chars, consecutive, invalidations = 0, 0, 0, 0, 0
    action_since_read = False
    for name, _, result in calls:
        if not result or "error" in result:
            continue
        if name == "player_turn":
            current = result
            action_since_read = False
        update = result.get("stateUpdate")
        if name == "player_command":
            assert update, "Successful gameplay command omitted stateUpdate"
            consecutive += action_since_read
            action_since_read = True
        if not update:
            continue
        kinds[update["kind"]] += 1
        if update["kind"] == "turn_ended":
            current = None
            continue
        assert update["kind"] in {"delta", "full"}, "Live refresh failed"
        current = apply_overview_update(current, update)
        assert any(
            stamp(
                {**public_result(overview(snapshot)), "strategyReview": current["strategyReview"]},
                job_id,
                snapshot["epoch"],
                snapshot["revision"],
            )
            == current
            for snapshot in snapshots
        ), "Reconstructed overview differs from its permitted snapshot"
        matched += 1
        invalidations += len(update.get("invalidatedDetails", []))
        if update["kind"] == "delta":
            delta_chars += len(encoded(update))
            full_chars += len(
                encoded({"kind": "full", "overview": current, "invalidatedDetails": update["invalidatedDetails"]})
            )
            fields.update(list(update["set"]) + list(update["collections"]) + update["removed"])
    return {
        "updates": dict(kinds),
        "reconstructedSnapshots": matched,
        "commandsWithoutInterveningOverview": consecutive,
        "deltaChars": delta_chars,
        "equivalentFullUpdateChars": full_chars,
        "invalidatedInspections": invalidations,
        "changedSections": dict(fields),
    }


def verify(root, sessions):
    report = verify_results(root, sessions)
    record = json.loads((root / "discovery-run.json").read_text())
    assert record["purpose"] == "gameplay"
    with contextlib.closing(
        sqlite3.connect(f"file:{root / 'agent-services/Rome/civilization/memory.sqlite3'}?mode=ro", uri=True)
    ) as db:
        calls = [
            (name, json.loads(args), json.loads(result) if result else None)
            for name, args, result in db.execute(
                "SELECT name,arguments,result FROM calls WHERE job=? ORDER BY rowid",
                (record["activationId"],),
            )
        ]
        snapshots = [
            json.loads(body) for (body,) in db.execute("SELECT body FROM records WHERE key='observation:game'")
        ]
    metrics = measure(calls, snapshots, record["activationId"])
    assert metrics["reconstructedSnapshots"] > 0 and metrics["commandsWithoutInterveningOverview"] > 0
    assert metrics["updates"].get("turn_ended") == 1
    turn = next(
        row
        for row in map(json.loads, (root / "telemetry/turns.jsonl").read_text().splitlines())
        if record["activationId"] in row["jobs"]
    )
    assert turn["accountingComplete"] and turn["turn"]["status"] == "completed"
    with contextlib.closing(
        sqlite3.connect(f"file:{root / 'agent-services/Rome/telemetry/accounting.sqlite3'}?mode=ro", uri=True)
    ) as db:
        local = json.loads(db.execute("SELECT payload FROM jobs WHERE id=?", (record["activationId"],)).fetchone()[0])
    assert local["usage"] == record["usage"]
    assert local["telemetry"]["apiCalls"] == turn["agentCalls"]
    assert Counter(name for name, _, _ in calls) == {
        key.removeprefix("tool:"): value["calls"]
        for key, value in turn["agentCalls"].items()
        if key.startswith("tool:")
    }
    metrics["elapsedMs"] = local["telemetry"]["elapsedMs"]
    metrics["agentCalls"] = turn["agentCalls"]
    metrics["hostCalls"] = turn["hostCalls"]
    metrics["hostOverviewCalls"] = turn["hostCalls"]["tool:player_turn"]["calls"]
    report["stateUpdates"] = metrics
    write_private_json(root / "state-update-metrics.json", report)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("state_dir", type=Path)
    parser.add_argument("--sessions", type=Path, default=Path.home() / ".codex/sessions")
    args = parser.parse_args()
    report = verify(args.state_dir, args.sessions)
    print(
        json.dumps(
            {key: value for key, value in report["stateUpdates"].items() if key not in {"agentCalls", "hostCalls"}},
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
