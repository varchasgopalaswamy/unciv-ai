import json
from unittest.mock import Mock

import pytest

from civ_agent import cli
from civ_agent.civilization_state import CivilizationState
from civ_agent.experiment_status import classifier_status, experiment_status, memory_status
from civ_agent.game_config import GameConfig, load_config
from civ_agent.memory_store import RecordStore
from civ_agent.telemetry import JobAccounting, calls
from civ_agent.tls import write_private_json


def enabled_config():
    return GameConfig(agents={"native_advice_enabled": True, "decisions_enabled": True}).resolved()


def test_status_is_read_only_and_reports_advice_queues_and_classifier_failure(tmp_path, monkeypatch, capsys):
    config = enabled_config()
    write_private_json(tmp_path / "resolved.json", config.model_dump())
    write_private_json(
        tmp_path / "container.json", {"format": 1, "id": "1234567890", "auth_root": str(tmp_path / "auth")}
    )
    directory = tmp_path / "agent-services/Rome"
    state = CivilizationState(directory / "civilization", "Rome")
    state.bind("epoch")
    state.start_job("running", "gameplay", {})
    with state.transaction() as db:
        state._set(db, "routing:events", {"1": {"receivedAt": 100}, "2": {"choice": "respond_now"}})
        store = RecordStore(db)
        store.write(
            "guidance-one",
            "guidance",
            {
                "type": "exposure",
                "epoch": "epoch",
                "revision": "rev",
                "available": False,
                "recommendations": [],
                "limitations": ["Native advice unavailable: capture deadline exceeded."],
            },
        )
        for status in ("queued", "omitted", "sent"):
            store.write(
                status,
                "delivery",
                {
                    "deliveryId": status,
                    "status": status,
                    "deadline": 130,
                    "createdAt": 100,
                    "payload": {"text": "PRIVATE TEXT MUST NOT APPEAR IN STATUS"},
                },
            )
    accounting = JobAccounting(directory / "telemetry")
    accounting.start("running", "epoch", True)
    original_phase = accounting.pending()[0]["telemetry"]["phase"]
    call = accounting.begin_call("running", "native_advice", {})
    accounting.end_call(call, "success", False)
    with accounting.connect() as db:
        db.execute(calls.update().where(calls.c.id == call).values(elapsed=42))
    accounting.classifier(
        "outgoing",
        {
            "choice": "send_now",
            "fallback": "service_failure",
            "latencyMs": 14,
            "providerError": {"kind": "authentication", "httpStatus": 401},
            "usage": {"input_tokens": 9},
        },
    )
    health = Mock(return_value={"status": "ready", "upstreamStatus": "unavailable"})
    monkeypatch.setattr("civ_agent.container_supervisor.routing_broker_health", health)
    report = experiment_status(tmp_path, config, json.loads((tmp_path / "container.json").read_text()), now=140)
    player = report["players"][0]
    assert player["memory"]["advice"]["status"] == "unavailable"
    assert "deadline" in player["memory"]["advice"]["unavailableReasons"][0]
    assert player["memory"]["routing"]["outstanding"] == {"queued": 1, "omitted": 1}
    assert player["memory"]["routing"]["oldestWaitSeconds"] == 40
    assert player["memory"]["routing"]["overdueDeliveries"] == 1
    assert player["memory"]["routing"]["incomingUnclassifiedOverdue"] == 1
    assert player["classifier"]["status"] == "degraded"
    assert player["classifier"]["latest"]["providerError"] == {"kind": "authentication", "httpStatus": 401}
    assert player["classifier"]["lastAdviceCallLatencyMs"] == 42
    assert player["classifier"]["usage"] == {"input_tokens": 9}
    assert "PRIVATE TEXT" not in json.dumps(report)
    # Reading must not invoke service constructors that interrupt running jobs.
    with state.transaction() as db:
        assert db.exec_driver_sql("SELECT status FROM jobs WHERE id='running'").scalar_one() == "RUNNING"
    assert accounting.pending()[0]["telemetry"]["phase"] == original_phase
    assert cli.main(["game", "status", "--state-dir", str(tmp_path)]) == 0
    assert json.loads(capsys.readouterr().out)["experiments"]["players"][0]["classifier"]["calls"] == 1


def test_absent_or_disabled_status_does_not_create_files_or_contact_broker(tmp_path, monkeypatch):
    health = Mock(side_effect=AssertionError("Disabled status must not query broker"))
    monkeypatch.setattr("civ_agent.container_supervisor.routing_broker_health", health)
    root = tmp_path / "missing"
    result = experiment_status(root, GameConfig().resolved(), {})
    assert not result["players"] and result["settings"]["decisions_enabled"] is False
    assert not root.exists()
    assert memory_status(root / "memory.sqlite3", now=0)["status"] == "waiting_for_first_observation"
    assert classifier_status(root / "accounting.sqlite3")["calls"] == 0
    assert not root.exists()


def test_trial_is_bounded_and_enables_both_experiments():
    from pathlib import Path

    config = load_config(Path(__file__).parents[1] / "examples/native-routing/trial.toml").resolved()
    assert config.agents.native_advice_enabled and config.agents.decisions_enabled
    assert config.limits.max_turns == 20 and config.limits.max_seconds == 7200
    assert len(config.agent_players) == 2 and config.human is None


@pytest.mark.parametrize("malformed", ["delivery", "routing", "classifier"])
def test_status_remains_usable_when_diagnostic_records_have_invalid_shapes(tmp_path, monkeypatch, malformed):
    from civ_agent.telemetry import classifier_calls

    config = enabled_config()
    directory = tmp_path / "agent-services/Rome"
    state = CivilizationState(directory / "civilization", "Rome")
    state.bind("epoch")
    if malformed == "classifier":
        accounting = JobAccounting(directory / "telemetry")
        with accounting.connect() as db:
            db.execute(classifier_calls.insert().values(id="bad", payload='{"choice":"send_now"}'))
    else:
        with state.transaction() as db:
            if malformed == "routing":
                state._set(db, "routing:events", ["not a routing state map"])
            else:
                RecordStore(db).write("broken-delivery", "delivery", {"status": "queued", "deliveryId": "id"})
    monkeypatch.setattr("civ_agent.container_supervisor.routing_broker_health", lambda _: {"status": "ready"})
    report = experiment_status(tmp_path, config, {}, now=140)
    key = "classifier" if malformed == "classifier" else "memory"
    assert report["players"][0][key] == {"status": "unavailable", "reason": "diagnostics_unreadable"}
