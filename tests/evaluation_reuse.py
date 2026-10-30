"""Reuse paid controls only when their implementation, inputs and settings match."""

import hashlib
import json
from pathlib import Path

from tests.evaluation_sources import tree_hash
from tests.report_role_contexts import job_accounting


def read(path):
    return json.loads(path.read_text())


def guide_hash(root):
    digest = hashlib.sha256()
    for path in sorted(root.rglob("*.md")):
        digest.update(str(path.relative_to(root)).encode() + b"\0" + path.read_bytes())
    return digest.hexdigest()


def reusable_controls(previous, *, variant, source, prepared, manifest, model, repetitions, engine):
    source_hash = tree_hash(source / "civ_agent")
    setup, suite = read(previous / "commands.json"), read(previous / model / "suite.json")
    expected = {
        "model": model,
        "reasoningEffort": "medium",
        "strategyGuide": True,
        "civilizationGuide": True,
        "reviewInterval": 10,
        "fast": False,
        "freshPrivateMemory": True,
        "memorySeeds": {},
        "repetitions": repetitions,
        "manifest": manifest,
    }
    if any(suite.get(key) != value for key, value in expected.items()):
        raise ValueError("Reused controls must match settings, positions and empty initial memory")
    if (
        setup.get("nativeAdvice") is not False
        or setup.get("adaptiveEffort") is not False
        or setup.get("roleSchedules") != {"economy": 5, "military": 5, "review": 10}
        or setup.get("sources", {}).get(variant, {}).get("sha256") != source_hash
        or suite.get("sources", {}).get(variant, {}).get("sha256") != source_hash
    ):
        raise ValueError("Reused controls must match source, advice and role schedules")
    original = Path(setup["sources"][variant]["path"]) / "civ_agent"
    guides = guide_hash(source / "civ_agent")
    if not original.is_dir() or tree_hash(original) != source_hash or guide_hash(original) != guides:
        raise ValueError("Reused controls must retain the matching frozen source and guide resources")
    wanted = {(case["id"], repetition) for case in manifest["positions"] for repetition in range(1, repetitions + 1)}
    selected = []
    for row in read(previous / model / "results.json"):
        if row["variant"] != variant or (row["case"], row["repetition"]) not in wanted:
            continue
        if not row["completedEvaluation"] or row.get("completedRounds") != 10 or row.get("expectedRounds") != 10:
            raise ValueError("Reused controls must have completed all ten rounds")
        path = previous / model / row["id"]
        run = read(path / "live-run.json")
        checkpoint = hashlib.sha256((prepared / row["case"] / "exploration.json").read_bytes()).hexdigest()
        if (
            run["pythonSourceSha256"] != source_hash
            or run["engineRevision"] != engine
            or run["checkpointSha256"] != checkpoint
        ):
            raise ValueError("Reused control source, engine or prepared checkpoint differs")
        accounting = job_accounting(path)
        if not accounting or not all(role["usageComplete"] for role in accounting.values()):
            raise ValueError("Reused controls require complete all-role usage accounting")
        selected.append(
            {
                **row,
                "id": f"{row['case']}-baseline-{row['repetition']}",
                "variant": "baseline",
                "sourceTrialId": row["id"],
                "sourceSuite": str(previous),
                "reused": True,
                "checkpointSha256": checkpoint,
                "guideResourcesSha256": guides,
            }
        )
    if len(selected) != len(wanted) or {(r["case"], r["repetition"]) for r in selected} != wanted:
        raise ValueError("Missing or duplicate matching completed controls")
    return selected
