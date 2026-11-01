import hashlib
import json
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest

from civ_agent.monitoring import MonitorStore, path_for
from tests import live_strategy_positions as suite
from tests.live_saved_position import copy_position_checkpoint
from tests.live_strategy_positions import (
    evaluation_status,
    freeze_memory_seeds,
    provider_usage_exhausted,
    reported_model_tokens,
    trial_schedule,
    validate_case,
)
from tests.report_strategy_positions import native_position


@pytest.mark.parametrize(
    "location,expected",
    [
        (None, {"x": 0, "y": 0}),
        ({"x": -6}, {"x": -6, "y": 0}),
        ({"y": 3}, {"x": 0, "y": 3}),
        ({"x": -2, "y": 1}, {"x": -2, "y": 1}),
    ],
)
def test_native_report_restores_omitted_default_city_coordinates(tmp_path, location, expected):
    city = {"id": "captured-capital", "name": "Paris"}
    if location is not None:
        city["location"] = location
    game = {
        "civilizations": [{"civID": "Persia", "cities": [city], "tech": {}}],
        "tileMap": {"tileList": []},
    }
    path = tmp_path / "checkpoint.json"
    path.write_text(json.dumps({"gameJson": json.dumps(game)}))
    position = native_position(path, ["Persia"])[0]
    assert position["cities"][0]["name"] == "Paris"
    assert position["cities"][0]["position"] == expected


def test_saved_position_copy_preserves_checkpoint_accounting_and_committed_wal(tmp_path):
    source = tmp_path / "prepared" / "exploration.json"
    source.parent.mkdir()
    source.write_text(json.dumps({"formatVersion": 9, "accountingId": "fixture-account"}))
    accounting = source.with_name(source.name + ".accounting.sqlite3")
    with sqlite3.connect(accounting) as writer:
        writer.execute("PRAGMA journal_mode=WAL")
        writer.execute("CREATE TABLE evidence (value TEXT)")
        writer.execute("INSERT INTO evidence VALUES ('retained committed receipt')")
        writer.commit()
        target = tmp_path / "isolated"
        target.mkdir()
        copied = copy_position_checkpoint(source, target)
        assert copied["accountingId"] == "fixture-account"
        assert json.loads((target / "exploration.json").read_text()) == copied
        with sqlite3.connect(target / "exploration.json.accounting.sqlite3") as reader:
            assert reader.execute("SELECT value FROM evidence").fetchone()[0] == "retained committed receipt"


def test_saved_position_refuses_a_modern_checkpoint_without_its_accounting(tmp_path):
    source = tmp_path / "prepared.json"
    source.write_text(json.dumps({"formatVersion": 9, "accountingId": "fixture-account"}))
    target = tmp_path / "isolated"
    target.mkdir()
    with pytest.raises(ValueError, match="missing its checkpoint accounting"):
        copy_position_checkpoint(source, target)
    assert not (target / "exploration.json").exists()


def test_successful_process_exit_does_not_hide_an_agent_stopping_without_handoff(tmp_path):
    store = MonitorStore(path_for(tmp_path))
    store.put("state", "run", {"status": "paused", "reason": "Agent returned without a committed handoff"})
    finished = {"turnId": "epoch/0", "turn": {"status": "completed"}, "accountingComplete": True}
    unfinished = {"turnId": "epoch/1", "turn": {"status": "active"}, "accountingComplete": False}
    for row in (finished, finished, unfinished):
        store.put("turns", row["turnId"], row)
    partial = evaluation_status(tmp_path, 3, 0)
    assert partial["completedRounds"] == 1 and not partial["completedEvaluation"]
    assert partial["runnerStatus"] == "paused"
    store.put("state", "run", {"status": "budget_exhausted"})
    rows = [{**finished, "turnId": f"epoch/{index}"} for index in range(3)]
    for row in rows:
        store.put("turns", row["turnId"], row)
    assert evaluation_status(tmp_path, 3, 0)["completedEvaluation"]
    rows[-1]["accountingComplete"] = False
    store.put("turns", rows[-1]["turnId"], rows[-1])
    assert not evaluation_status(tmp_path, 3, 0)["completedEvaluation"]


def test_trials_rotate_implementations_and_keep_every_repetition():
    trials = trial_schedule([{"id": "science"}, {"id": "culture"}], ["facts", "full"], 3)
    assert len({t["id"] for t in trials}) == 12
    assert [t["variant"] for t in trials if t["case"]["id"] == "science"] == [
        "facts",
        "full",
        "full",
        "facts",
        "facts",
        "full",
    ]
    assert trial_schedule([{"id": "science"}], ["current"], 1)[0]["id"] == "science"


def test_suite_freezes_one_seed_for_all_variants_and_retains_original_provenance(tmp_path):
    manifest = tmp_path / "prepared" / "manifest.json"
    manifest.parent.mkdir()
    source = manifest.parent / "prior-war.json"
    raw = b'{"civilization":"Rome","records":[],"wars":[]}'
    source.write_bytes(raw)
    case = {"id": "counteroffensive", "civilization": "Rome", "memorySeed": "prior-war.json"}
    frozen = freeze_memory_seeds([case], manifest, tmp_path / "suite")[case["id"]]
    assert frozen["source"] == str(source.resolve())
    assert frozen["sha256"] == hashlib.sha256(raw).hexdigest()
    source.write_text('{"civilization":"Rome","records":["changed after freeze"]}')
    assert Path(frozen["path"]).read_bytes() == raw


def test_suite_rejects_memory_seed_for_a_different_civilization(tmp_path):
    source = tmp_path / "seed.json"
    source.write_text('{"civilization":"France","records":[]}')
    case = {"id": "counteroffensive", "civilization": "Rome", "memorySeed": "seed.json"}
    with pytest.raises(ValueError, match="another civilization"):
        freeze_memory_seeds([case], tmp_path / "manifest.json", tmp_path / "suite")
    assert not (tmp_path / "suite").exists()


def test_case_preflight_does_not_mistake_a_queued_conversion_for_current_output():
    case = {"id": "conversion", "requiresScienceConversion": True}
    with pytest.raises(ValueError, match="active Science"):
        validate_case(case, {"cities": [{"constructionQueue": ["Library", "Science"]}]})
    validate_case(case, {"cities": [{"constructionQueue": ["Science"]}]})
    with pytest.raises(ValueError, match="cultural specialization"):
        validate_case({"id": "culture", "minimumCulturePerTurn": 80}, {"culture": {"culturePerTurn": 30}})


def test_usage_limit_detection_includes_reviews_and_ignores_failure_prose(tmp_path):
    store = MonitorStore(path_for(tmp_path))
    store.put("state", "run", {"status": "paused", "reason": "usageLimitExceeded"})
    assert not provider_usage_exhausted(tmp_path)
    activation = tmp_path / "agent-services/Rome/activations/review.json"
    activation.parent.mkdir(parents=True)
    activation.write_text(
        json.dumps(
            {"phase": "FAILED", "reviewProgress": {"modelStatus": {"state": "failed", "reason": "serverOverloaded"}}}
        )
    )
    assert not provider_usage_exhausted(tmp_path)
    activation.write_text(
        json.dumps(
            {"phase": "FAILED", "reviewProgress": {"modelStatus": {"state": "failed", "reason": "usageLimitExceeded"}}}
        )
    )
    assert provider_usage_exhausted(tmp_path)
    activation.write_text(
        json.dumps({"phase": "FAILED", "modelStatus": {"state": "failed", "reason": "usageLimitExceeded"}})
    )
    assert provider_usage_exhausted(tmp_path)
    store.engine.dispose()


def test_token_guard_counts_all_roles_and_job_revisions_only_once(tmp_path):
    store = MonitorStore(path_for(tmp_path))
    store.put("state", "run", {"usage": {"input_tokens": 100, "output_tokens": 10}})
    for number, purpose in enumerate(("gameplay", "review", "communication"), 1):
        store.put(
            "jobs",
            purpose,
            {
                "job": {
                    "purpose": purpose,
                    "usage": {
                        "input_tokens": number * 100,
                        "cached_input_tokens": number * 80,
                        "output_tokens": number * 10,
                    },
                }
            },
        )
    store.put("jobs", "military", {"job": {"purpose": "military", "phase": "STARTING", "usage": None}})
    store.put("jobs", "economy", {"job": {"purpose": "economy", "phase": "STARTING"}})
    assert reported_model_tokens(tmp_path) == 660
    store.put(
        "jobs",
        "review",
        {
            "job": {
                "purpose": "review",
                "phase": "FAILED",
                "usage": {"input_tokens": 250, "output_tokens": 25},
            }
        },
    )
    assert reported_model_tokens(tmp_path) == 715
    store.engine.dispose()


def test_suite_stops_launching_trials_after_provider_limit_and_retains_schedule(tmp_path, monkeypatch):
    prepared, root, manifest = tmp_path / "prepared", tmp_path / "runs", tmp_path / "manifest.json"
    cases = [{"id": "first", "rounds": 7}, {"id": "second", "rounds": 7}]
    manifest.write_text(json.dumps({"positions": cases}))
    for case in cases:
        directory = prepared / case["id"]
        directory.mkdir(parents=True)
        (directory / "initial-turn.json").write_text("{}")
    launched = []

    def launch(command, **kwargs):
        launched.append(command)
        trial = root / "first"
        store = MonitorStore(path_for(trial))
        store.put(
            "state",
            "run",
            {
                "status": "paused",
                "inFlight": {"modelStatus": {"state": "failed", "reason": "usageLimitExceeded"}},
            },
        )
        store.engine.dispose()
        return SimpleNamespace(returncode=0, poll=lambda: 0)

    monkeypatch.setattr(suite.subprocess, "Popen", launch)
    monkeypatch.setattr(
        suite.sys,
        "argv",
        [
            "saved-positions",
            "--prepared",
            str(prepared),
            "--state-dir",
            str(root),
            "--manifest",
            str(manifest),
            "--port",
            "13401",
            "--parallel",
            "1",
        ],
    )
    assert suite.main() == 2
    assert len(launched) == 1
    results = {row["id"]: row for row in json.loads((root / "results.json").read_text())}
    assert results["first"]["exitCode"] == 0 and not results["first"]["completedEvaluation"]
    assert results["first"]["providerFailure"] == "usageLimitExceeded"
    assert results["second"]["notStarted"] and not results["second"]["completedEvaluation"]
    assert "usageLimitExceeded" in results["second"]["reason"]


def test_suite_skips_completed_ids_without_relabeling_remaining_trials(tmp_path, monkeypatch):
    prepared, root, manifest = tmp_path / "prepared", tmp_path / "runs", tmp_path / "manifest.json"
    cases = [{"id": "first", "rounds": 1}, {"id": "second", "rounds": 1}]
    manifest.write_text(json.dumps({"positions": cases}))
    for case in cases:
        directory = prepared / case["id"]
        directory.mkdir(parents=True)
        (directory / "initial-turn.json").write_text("{}")
    launched = []

    def launch(command, **kwargs):
        trial = Path(command[command.index("--state-dir") + 1])
        launched.append(trial.name)
        store = MonitorStore(path_for(trial))
        store.put("state", "run", {"status": "budget_exhausted"})
        store.put("turns", "t1", {"turnId": "t1", "turn": {"status": "completed"}, "accountingComplete": True})
        store.engine.dispose()
        return SimpleNamespace(returncode=0, poll=lambda: 0)

    monkeypatch.setattr(suite.subprocess, "Popen", launch)
    monkeypatch.setattr(
        suite.sys,
        "argv",
        [
            "suite",
            "--prepared",
            str(prepared),
            "--state-dir",
            str(root),
            "--manifest",
            str(manifest),
            "--port",
            "13401",
            "--skip-trial",
            "first",
        ],
    )
    assert suite.main() == 0
    assert launched == ["second"]
    assert json.loads((root / "suite.json").read_text())["previouslyCompletedTrials"] == ["first"]
    result = json.loads((root / "results.json").read_text())
    assert len(result) == 1 and result[0]["id"] == "second" and result[0]["completedEvaluation"]
