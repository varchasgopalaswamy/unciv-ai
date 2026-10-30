"""Historical controls never substitute different boards, settings or partial usage."""

import hashlib
import json

import pytest

from tests.evaluation_reuse import reusable_controls
from tests.evaluation_sources import tree_hash


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value))


def fixture(tmp_path):
    previous, source, prepared = (tmp_path / name for name in ("previous", "source", "prepared"))
    (source / "civ_agent").mkdir(parents=True)
    (source / "civ_agent/source.py").write_text("# frozen control\n")
    (source / "civ_agent/strategy_guide.md").write_text("Guide\n")
    original = previous / "frozen"
    (original / "civ_agent").mkdir(parents=True)
    (original / "civ_agent/source.py").write_text("# frozen control\n")
    (original / "civ_agent/strategy_guide.md").write_text("Guide\n")
    source_hash = tree_hash(source / "civ_agent")
    manifest = {"positions": [{"id": "position", "civilization": "Rome"}]}
    sources = {"candidate": {"path": str(original), "sha256": source_hash}}
    write(
        previous / "commands.json",
        {
            "sources": sources,
            "nativeAdvice": False,
            "adaptiveEffort": False,
            "roleSchedules": {"economy": 5, "military": 5, "review": 10},
        },
    )
    write(
        previous / "model/suite.json",
        {
            "model": "model",
            "reasoningEffort": "medium",
            "strategyGuide": True,
            "civilizationGuide": True,
            "reviewInterval": 10,
            "fast": False,
            "freshPrivateMemory": True,
            "memorySeeds": {},
            "repetitions": 1,
            "manifest": manifest,
            "sources": sources,
        },
    )
    write(prepared / "position/exploration.json", {"prepared": "board"})
    row = {
        "id": "position-candidate-1",
        "variant": "candidate",
        "case": "position",
        "repetition": 1,
        "completedEvaluation": True,
        "completedRounds": 10,
        "expectedRounds": 10,
    }
    write(previous / "model/results.json", [row])
    trial = previous / "model" / row["id"]
    write(
        trial / "live-run.json",
        {
            "pythonSourceSha256": source_hash,
            "engineRevision": "engine",
            "checkpointSha256": hashlib.sha256((prepared / "position/exploration.json").read_bytes()).hexdigest(),
        },
    )
    write(
        trial / "agent-services/Rome/activations/job.json",
        {
            "purpose": "gameplay",
            "phase": "COMPLETED",
            "usageComplete": True,
            "usage": {"input_tokens": 100, "cached_input_tokens": 80, "output_tokens": 2},
            "modelRequests": 1,
        },
    )
    args = dict(
        variant="candidate",
        source=source,
        prepared=prepared,
        manifest=manifest,
        model="model",
        repetitions=1,
        engine="engine",
    )
    return previous, trial, args


def test_completed_identical_control_is_reused_with_original_provenance(tmp_path):
    previous, _, args = fixture(tmp_path)
    rows = reusable_controls(previous, **args)
    assert len(rows) == 1 and rows[0]["id"] == "position-baseline-1"
    assert rows[0]["sourceTrialId"] == "position-candidate-1" and rows[0]["reused"]


@pytest.mark.parametrize(
    "changed", ["board", "engine", "source", "guide", "effort", "memory", "schedule", "incomplete", "usage"]
)
def test_incompatible_control_is_rejected_before_inference(tmp_path, changed):
    previous, trial, args = fixture(tmp_path)
    if changed == "board":
        write(args["prepared"] / "position/exploration.json", {"prepared": "different board"})
    elif changed == "engine":
        args["engine"] = "different-engine"
    elif changed == "source":
        (args["source"] / "civ_agent/source.py").write_text("# changed\n")
    elif changed == "guide":
        (args["source"] / "civ_agent/strategy_guide.md").write_text("Changed guidance\n")
    else:
        path = {
            "effort": previous / "model/suite.json",
            "memory": previous / "model/suite.json",
            "schedule": previous / "commands.json",
            "incomplete": previous / "model/results.json",
            "usage": trial / "agent-services/Rome/activations/job.json",
        }[changed]
        value = json.loads(path.read_text())
        if changed == "effort":
            value["reasoningEffort"] = "low"
        elif changed == "memory":
            value["memorySeeds"] = {"Rome": "other-notebook"}
        elif changed == "schedule":
            value["roleSchedules"]["economy"] = 1
        elif changed == "incomplete":
            value[0]["completedRounds"] = 9
        else:
            value["usageComplete"] = False
        write(path, value)
    with pytest.raises(ValueError, match="Reused controls? must|Reused controls? source|Reused controls? require"):
        reusable_controls(previous, **args)
