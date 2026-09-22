import json

from tests.live_strategy_positions import evaluation_status


def test_successful_process_exit_does_not_hide_an_agent_stopping_without_handoff(tmp_path):
    (tmp_path / "runner").mkdir()
    (tmp_path / "telemetry").mkdir()
    state = tmp_path / "runner/run.json"
    turns = tmp_path / "telemetry/turns.jsonl"
    state.write_text(json.dumps({"status": "paused", "reason": "Agent returned without a committed handoff"}))
    finished = {"turnId": "epoch/0", "turn": {"status": "completed"}, "accountingComplete": True}
    unfinished = {"turnId": "epoch/1", "turn": {"status": "active"}, "accountingComplete": False}
    # Repeated telemetry updates are one turn, not extra completed rounds.
    turns.write_text("\n".join(map(json.dumps, [finished, finished, unfinished])))
    partial = evaluation_status(tmp_path, 3, 0)
    assert partial["completedRounds"] == 1 and not partial["completedEvaluation"]
    assert partial["runnerStatus"] == "paused"
    state.write_text(json.dumps({"status": "budget_exhausted"}))
    rows = [{**finished, "turnId": f"epoch/{index}"} for index in range(3)]
    turns.write_text("\n".join(map(json.dumps, rows)))
    assert evaluation_status(tmp_path, 3, 0)["completedEvaluation"]
    rows[-1]["accountingComplete"] = False
    turns.write_text("\n".join(map(json.dumps, rows)))
    assert not evaluation_status(tmp_path, 3, 0)["completedEvaluation"]
