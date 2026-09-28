from civ_agent.monitoring import MonitorStore, path_for
from tests.live_strategy_positions import evaluation_status


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
