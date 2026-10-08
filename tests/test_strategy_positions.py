import json
from types import SimpleNamespace

import pytest

from civ_agent.monitoring import MonitorStore, path_for
from tests import live_strategy_positions as suite
from tests.live_strategy_positions import (
    evaluation_status,
    provider_usage_exhausted,
    reported_model_tokens,
    trial_schedule,
    validate_case,
)


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
