"""Execution profiles belong to player identities, then freeze in native seats."""

import json

import pytest
from pydantic import ValidationError

from civ_agent.execution_policy import ExecutionPolicy, legacy_execution_policy
from civ_agent.game_config import GameConfig, Player, ResolvedGameConfig
from civ_agent.orchestration.managed_player import NativePlayerSession, NativePlayerSettings


def test_missing_player_override_preserves_legacy_player_bytes_and_inherits_global_policy():
    player = Player(name="Agent")
    assert player.model_dump(mode="json") == {
        "name": "Agent",
        "controller": "agent",
        "economy_model": None,
        "military_model": None,
        "provider": None,
        "model": None,
        "reasoning_effort": None,
        "communication_reasoning_effort": None,
    }
    for policy in (legacy_execution_policy(), ExecutionPolicy(), ExecutionPolicy.current()):
        config = GameConfig.model_validate({"agents": {"execution_policy": policy.model_dump(mode="json")}})
        settings = NativePlayerSettings.model_validate(config.settings(config.players[0]))
        assert settings.execution_policy == policy
        assert "execution_policy" not in config.players[0].model_dump(mode="json")


def test_mixed_policies_follow_identity_through_shuffle_and_resolved_round_trip():
    direct, candidate = ExecutionPolicy(), ExecutionPolicy.current()
    config = GameConfig.model_validate(
        {
            "game": {
                "civilizations": ["France", "China", "Persia"],
                "master_seed": 42,
                "assignment_seed": 7,
                "randomize": True,
            },
            "agents": {"model": "gpt-6.1-terra", "execution_policy": candidate.model_dump(mode="json")},
            "players": [
                {"name": "Direct", "execution_policy": direct.model_dump(mode="json")},
                {"name": "Code Mode", "execution_policy": candidate.model_dump(mode="json")},
                {"name": "Inherited"},
            ],
        }
    ).resolved()
    restored = ResolvedGameConfig.model_validate_json(config.model_dump_json())
    assert restored.assignments == config.assignments
    assert [p.name for p in restored.assigned_players] != [p.name for p in restored.players]
    policies = {"Direct": direct, "Code Mode": candidate, "Inherited": candidate}
    for assigned in restored.assigned_players:
        # This is the same settings boundary used by runtime.controller.build_players.
        native = NativePlayerSettings.model_validate(restored.settings(assigned))
        assert native.execution_policy == policies[assigned.name]
        assert native.model == "gpt-6.1-terra"
        assert native.execution_policy.code_mode.enabled == (assigned.name != "Direct")
    assert restored.model_dump_json() == config.model_dump_json()


def test_explicit_legacy_override_wins_over_global_candidate_and_freezes_in_native_session():
    config = GameConfig.model_validate(
        {
            "agents": {"execution_policy": ExecutionPolicy.current().model_dump(mode="json")},
            "players": [
                {"name": "Legacy", "execution_policy": {"version": 0}},
                {"name": "Current"},
                {"name": "Other"},
                {"name": "Native", "controller": "built_in_ai"},
            ],
        }
    ).resolved()
    assigned = next(p for p in config.agent_players if p.name == "Legacy")
    native = NativePlayerSettings.model_validate(config.settings(assigned))
    session = NativePlayerSession(game="mixed", player="p0", settings=native)
    frozen = NativePlayerSession.model_validate_json(session.model_dump_json())
    assert frozen.settings.execution_policy == legacy_execution_policy()
    assert not frozen.settings.execution_policy.code_mode.enabled
    assert "execution_policy" not in json.loads(frozen.model_dump_json())["settings"]
    with pytest.raises(ValidationError, match="frozen_instance"):
        frozen.settings.execution_policy.__setattr__("version", 2)
    assert config.agents.execution_policy == ExecutionPolicy.current()


@pytest.mark.parametrize("controller", ["human", "built_in_ai"])
def test_non_agent_players_reject_execution_overrides(controller):
    with pytest.raises(ValidationError, match="Only agent players have execution policy settings"):
        Player.model_validate(
            {
                "name": "Non-agent",
                "controller": controller,
                "execution_policy": ExecutionPolicy().model_dump(mode="json"),
            }
        )


def test_invalid_or_hybrid_player_policy_is_rejected_before_resolution():
    with pytest.raises(ValidationError):
        Player.model_validate({"name": "Invalid", "execution_policy": {"version": 2, "hybrid": True}})
