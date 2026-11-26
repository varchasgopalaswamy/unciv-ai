from unittest.mock import Mock

from civ_agent.experiment_status import classifier_status, experiment_status, memory_status
from civ_agent.game_config import GameConfig, load_config


def enabled_config():
    return GameConfig(agents={"native_advice_enabled": True, "decisions_enabled": True}).resolved()


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
