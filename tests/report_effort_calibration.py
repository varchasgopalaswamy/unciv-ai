"""Blind answer export and complete accounting for the opt-in effort screen."""

import argparse
import hashlib
import json
import sqlite3
from collections import Counter, defaultdict
from pathlib import Path

from civ_agent.protocol import JsonObject
from civ_agent.tls import write_private_json
from tests.effort_calibration import digest, read


def blind_id(trial_id):
    return hashlib.sha256(("effort-blind-v1:" + trial_id).encode()).hexdigest()[:12]


def export(root):
    manifest = read(root / "manifest.json")
    groups = defaultdict(list)
    mapping = {}
    for row in manifest["schedule"]:
        destination = root / "trials" / row["id"]
        if not (destination / "result.json").exists():
            raise ValueError("Finish every scheduled cell before blind export")
        result = read(destination / "result.json")
        identity = blind_id(row["id"])
        if identity in mapping:
            raise ValueError("Blind identity collision")
        mapping[identity] = row["id"]
        groups[row["case"]].append(
            {
                "id": identity,
                "valid": result["valid"],
                "decision": read(destination / "decision.json") if result["valid"] else None,
            }
        )
    (root / "blind").mkdir(exist_ok=True)
    for case, rows in groups.items():
        write_private_json(root / "blind" / (case + ".json"), sorted(rows, key=lambda r: r["id"]))
    write_private_json(root / "blind-mapping.json", mapping)
    print(json.dumps({"cases": len(groups), "answers": len(mapping)}))


def verify(root):
    manifest = read(root / "manifest.json")
    cases = {c["id"]: read(root / "cases" / c["id"] / "context.json") for c in manifest["cases"]}
    for path, expected in read(root / "frozen.json").items():
        assert hashlib.sha256((root / path).read_bytes()).hexdigest() == expected
    rows = []
    request_ids = []
    for scheduled in manifest["schedule"]:
        row = read(root / "trials" / scheduled["id"] / "result.json")
        assert all(row[k] == scheduled[k] for k in scheduled)
        case = cases[row["case"]]
        for wire in row.get("wire", []):
            assert wire["model"] == row["model"] and wire["reasoning"]["effort"] == row["effort"]
            assert wire["inputSha256"] == case["inputSha256"] == digest(case["input"])
            assert wire["instructionsSha256"] == digest(case["instructions"])
            assert wire["schemaFingerprint"] == case["schema"]["fingerprint"]
            assert wire["executableTools"] == 0
        assert row["dispatchedOperations"] == 0
        request_ids.extend(r["requestId"] for r in row["requests"])
        rows.append(row)
    with sqlite3.connect(f"file:{root}/requests.sqlite?mode=ro", uri=True) as db:
        ledger_ids = {row[0] for row in db.execute("select id from requests")}
    assert len(request_ids) == len(set(request_ids))
    assert set(request_ids) == ledger_ids
    assert len(ledger_ids) <= manifest["requestCeilingIncludingRetries"]
    return rows


def summarize(root):
    rows = verify(root)
    grades = read(root / "grades.json")
    assert set(grades) == {blind_id(row["id"]) for row in rows}
    groups = defaultdict(list)
    for row in rows:
        grade = grades[blind_id(row["id"])]
        assert grade["assessment"] in {"semantic_error", "no_observed_semantic_error", "unresolved", "invalid"}
        row["grade"] = grade
        groups[(row["model"], row["effort"])].append(row)
    summaries: list[JsonObject] = []
    for (model, effort), cells in groups.items():
        tokens = Counter()
        for row in cells:
            usage = row.get("usage", {}).get("total")
            if usage:
                assert usage["inputTokens"] >= usage["cachedInputTokens"]
                tokens.update(
                    {k: usage[k] for k in ("inputTokens", "cachedInputTokens", "outputTokens", "reasoningOutputTokens")}
                )
        tokens["uncachedInputTokens"] = tokens["inputTokens"] - tokens["cachedInputTokens"]
        tokens["totalTokens"] = tokens["inputTokens"] + tokens["outputTokens"]
        summaries.append(
            {
                "model": model,
                "effort": effort,
                "cells": len(cells),
                "valid": sum(bool(r["valid"]) for r in cells),
                "requests": sum(len(r["requests"]) for r in cells),
                "requestsWithoutReportedUsage": sum(
                    len(r["requests"]) - bool(r.get("usage", {}).get("total")) for r in cells
                ),
                "usage": dict(tokens),
                "assessments": dict(Counter(r["grade"]["assessment"] for r in cells)),
                "decisionKinds": dict(Counter(r.get("kind", "invalid") for r in cells)),
                "cases": {
                    case: [r["grade"] for r in cells if r["case"] == case]
                    for case in sorted({r["case"] for r in cells})
                },
            }
        )
    total = Counter()
    for group in summaries:
        total.update(group["usage"])
    report = {
        "scope": "single-decision screen, not complete simulated turns",
        "trials": len(rows),
        "providerRequests": sum(len(r["requests"]) for r in rows),
        "usage": dict(total),
        "groups": summaries,
        "rows": rows,
        "verification": "Frozen context/schema/model/effort matches; no executable tools or dispatch; every request reconciles with the budget ledger",
    }
    write_private_json(root / "evaluation.json", report)
    print(json.dumps({k: report[k] for k in ("trials", "providerRequests", "usage")}, indent=2))
    for group in sorted(
        summaries, key=lambda r: (r["model"], ("low", "medium", "high", "xhigh", "max").index(r["effort"]))
    ):
        print(json.dumps({k: group[k] for k in ("model", "effort", "assessments", "decisionKinds", "usage")}))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("export-blind", "summarize", "verify"))
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    root = args.output.resolve()
    if args.action == "export-blind":
        export(root)
    elif args.action == "verify":
        rows = verify(root)
        print(json.dumps({"verifiedTrials": len(rows)}))
    else:
        summarize(root)


if __name__ == "__main__":
    main()
