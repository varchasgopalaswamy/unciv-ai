"""Opt-in civilization advice is player-specific, ordered, and validated early."""

import asyncio
import copy
import hashlib
import json
import os
from unittest.mock import Mock

import pytest

from civ_agent import container_runtime, interactive_session
from civ_agent.agent_service import AgentService
from civ_agent.benchmark import generate_runs, seeded_input
from civ_agent.civilization_guide import (
    CIVILIZATION_GUIDES,
    UnsupportedCivilizationGuideError,
    civilization_reference,
)
from civ_agent.commands import agent_server, game
from civ_agent.game_config import GameConfig, ResolvedGameConfig, load_config
from civ_agent.review_grounding import ReviewEvidence
from civ_agent.startup_context import startup_prompt, startup_text
from civ_agent.strategic_review import advice_prompt
from civ_agent.strategy_guide import strategy_reference
from civ_agent.turn_archive import civilization_directory_name
from tests.test_opening_review import OpeningHost
from tests.test_strategic_adviser import ReviewFactory
from tests.test_victory_snapshot import position

CIVILIZATIONS = ("Inca", "France", "Greece", "Arabia", "China", "Persia")


def prompt_for(civilization, purpose, *, strategy_guide=False, civilization_guide=False, evidence=None):
    reads = {
        "player_turn": {"turn": 10, "civilization": civilization},
        "chat_read": {"civilization": civilization, "messages": ["PRIVATE_CHAT"]},
        "victory_progress": position(10),
    }
    if purpose == "review":
        return advice_prompt(
            {
                "catalogue": [],
                "civilization": civilization,
                "turn": 10,
                "reads": reads,
                "observation": {"turn": 10},
                "actions": {"entries": []},
            },
            evidence=evidence,
            strategy_guide=strategy_guide,
            civilization_guide=civilization_guide,
        )
    prepared = {"coreTools": [], "readInstructions": {}, "reads": reads, "memoryContext": "PRIVATE_MEMORY"}
    before = copy.deepcopy(prepared)
    options = {
        "strategy_guide": strategy_guide,
        "civilization_guide": civilization_guide,
        "civilization": civilization,
    }
    if purpose == "interactive":
        prompt = startup_text(prepared, **options)
    else:
        prompt = startup_prompt(prepared, purpose, "Choose next steps", **options)
    assert prepared == before
    return prompt


@pytest.mark.parametrize("civilization", CIVILIZATIONS)
@pytest.mark.parametrize("purpose", ["gameplay", "communication", "review", "interactive"])
@pytest.mark.parametrize("strategy_guide", [False, True])
@pytest.mark.parametrize("civilization_guide", [False, True])
def test_only_own_advice_appears_once_after_general_guide(civilization, purpose, strategy_guide, civilization_guide):
    evidence = ReviewEvidence({"observation": {"turn": 10}})
    prompt = prompt_for(
        civilization, purpose, strategy_guide=strategy_guide, civilization_guide=civilization_guide, evidence=evidence
    )
    baseline = prompt_for(civilization, purpose, evidence=evidence)
    text = prompt if isinstance(prompt, str) else prompt.user
    original = baseline if isinstance(baseline, str) else baseline.user
    reference = civilization_reference(civilization)
    assert text.endswith(original)
    assert text.count(strategy_reference()) == int(strategy_guide)
    assert text.count(reference) == int(civilization_guide)
    assert all(civilization_reference(other) not in text for other in CIVILIZATIONS if other != civilization)
    if civilization_guide:
        assert text.index(reference) < text.index("PRIVATE_CHAT")
        if strategy_guide:
            assert text.index(strategy_reference()) < text.index(reference)
    if not isinstance(prompt, str):
        assert not isinstance(baseline, str)
        assert prompt.instructions == baseline.instructions
        assert reference not in prompt.instructions
        assert prompt.metadata()["civilizationGuide"] == {
            "enabled": civilization_guide,
            "characters": len(reference) if civilization_guide else 0,
            "sha256": hashlib.sha256(reference.encode()).hexdigest() if civilization_guide else None,
        }


@pytest.mark.parametrize("purpose", ["gameplay", "communication", "review", "interactive"])
@pytest.mark.parametrize("civilization", ["Rome", "Egypt", "../france", None])
def test_unimplemented_civilization_raises_only_when_enabled(purpose, civilization):
    prompt_for(civilization, purpose, strategy_guide=True)
    with pytest.raises(UnsupportedCivilizationGuideError, match="civilization_guide is not implemented"):
        prompt_for(civilization, purpose, civilization_guide=True)


def test_packaged_advice_loads_outside_checkout_and_is_optional(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    civilization_reference.cache_clear()
    assert set(CIVILIZATION_GUIDES) == set(CIVILIZATIONS)
    for civilization in CIVILIZATIONS:
        reference = civilization_reference(civilization)
        assert reference.startswith(f"# Optional civilization strategy reference: {civilization}\n")
        assert "suggestions, not instructions, rules, or a checklist" in reference
        assert "current engine observations take precedence" in reference
        assert "profitable conquest" in reference and "peaceful development" in reference


def test_toml_and_cli_flags_are_independent_and_survive_batch_round_trip(tmp_path):
    source = tmp_path / "input.toml"
    template = """
[game]
civilizations = ["Inca", "France", "Greece", "Arabia", "China", "Persia"]
randomize = true
master_seed = 42
[agents]
strategy_guide = true
civilization_guide = true
""" + "".join(f'\n[[players]]\nname = "Agent {index}"\n' for index in range(6))
    source.write_text(template)
    config = load_config(source)
    for row in generate_runs(config, 3):
        resolved = ResolvedGameConfig.model_validate(row["config"])
        assert resolved.agents.civilization_guide and resolved.agents.strategy_guide
        assert all(resolved.settings(player)["civilization_guide"] for player in resolved.agent_players)
        generated = tmp_path / f"{row['id']}.toml"
        generated.write_text(seeded_input(template, resolved))
        assert load_config(generated).resolved() == resolved
    disabled = game.resolve_options(game.parser().parse_args(["--config", str(source), "--no-civilization-guide"]))
    assert disabled.agents.strategy_guide and not disabled.agents.civilization_guide
    only_civ = game.resolve_options(
        game.parser().parse_args(["--config", str(source), "--no-strategy-guide", "--civilization-guide"])
    )
    assert only_civ.agents.civilization_guide and not only_civ.agents.strategy_guide
    assert not GameConfig().agents.civilization_guide


def test_validation_uses_final_assignments_and_ignores_non_agent_seats():
    for seed in range(8):
        config = GameConfig(
            game={"civilizations": ["Rome", "France"], "randomize": True, "assignment_seed": seed},
            players=[{"name": "Unciv", "controller": "built_in_ai"}, {"name": "Model"}],
        )
        baseline = config.resolved()
        baseline.agents.civilization_guide = True
        config.agents.civilization_guide = True
        if baseline.agent_players[0].civilization == "Rome":
            with pytest.raises(ValueError, match="civilization_guide is not implemented for 'Rome'"):
                config.resolved()
            with pytest.raises(ValueError, match="civilization_guide is not implemented for 'Rome'"):
                ResolvedGameConfig.model_validate_json(baseline.model_dump_json())
        else:
            assert config.resolved().agent_players[0].civilization == "France"
            assert ResolvedGameConfig.model_validate_json(baseline.model_dump_json()) == baseline
    human = GameConfig(
        game={"civilizations": ["Rome", "France"]},
        players=[{"name": "Human", "controller": "human"}, {"name": "Model"}],
        agents={"civilization_guide": True},
    ).resolved()
    assert human.human == "Rome"


def test_unsupported_game_fails_before_preflight_or_creating_state(tmp_path, monkeypatch, capsys):
    preflight = Mock()
    monkeypatch.setattr(game, "preflight", preflight)
    args = game.parser().parse_args(["--civilization-guide", "--state-dir", str(tmp_path / "match")])
    assert game.run(args) == 2
    assert "civilization_guide is not implemented for 'Rome'" in capsys.readouterr().err
    preflight.assert_not_called()
    assert not (tmp_path / "match").exists()


def test_unsupported_service_fails_before_state_or_model_work(tmp_path, capsys):
    factory = Mock()
    with pytest.raises(UnsupportedCivilizationGuideError, match="'Rome'"):
        AgentService(
            "Rome",
            tmp_path / "workspace",
            tmp_path / "service",
            "S" * 43,
            "https://localhost:8765/mcp",
            None,
            process_factory=factory,
            civilization_guide=True,
        )
    factory.assert_not_called()
    args = agent_server.parser().parse_args(["Rome", "--civilization-guide", "--state-dir", str(tmp_path / "match")])
    previous_umask = os.umask(0o077)
    try:
        assert agent_server.run(args) == 1
    finally:
        os.umask(previous_umask)
    assert "civilization_guide is not implemented for 'Rome'" in capsys.readouterr().err
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("enabled", [False, True])
def test_container_forwards_saved_setting_to_service_cli(tmp_path, monkeypatch, enabled):
    config = GameConfig(
        game={"civilizations": ["France"]},
        players=[{"name": "Model"}],
        agents={"civilization_guide": enabled},
    ).resolved()
    directory = tmp_path / "agent-services" / civilization_directory_name("France")
    directory.mkdir(parents=True)
    (directory / "container-settings.json").write_text(
        json.dumps({"civilization": "France", "hostname": "p0", "settings": config.settings(config.agent_players[0])})
    )
    monkeypatch.setattr(container_runtime, "ROOT", tmp_path)
    run = Mock(return_value=0)
    monkeypatch.setattr(agent_server, "run", run)
    assert container_runtime.agent("France") == 0
    assert run.call_args.args[0].civilization == "France"
    assert run.call_args.args[0].civilization_guide == enabled


def test_interactive_session_forwards_service_advice_without_model_calls(tmp_path, monkeypatch):
    class InteractiveHost(OpeningHost):
        async def tool(self, name, args):
            if name == "player_capabilities":
                return {"commands": ["END_TURN"], "queries": []}
            result = await super().tool(name, args)
            if name == "player_turn":
                result["phase"] = "ACTIVE"
            return result

    async def run():
        factory = ReviewFactory()
        service = AgentService(
            "France",
            tmp_path / "workspace",
            tmp_path / "service",
            "S" * 43,
            "https://localhost:8765/mcp",
            None,
            process_factory=factory,
            strategy_guide=True,
            civilization_guide=True,
        )
        factory.service = service
        host = InteractiveHost(factory)
        monkeypatch.setattr(interactive_session, "HostConnection", lambda *args, **kwargs: host)
        try:
            session = await interactive_session.open_session(
                service,
                {"hostUrl": service.mcp_url, "playerToken": "P" * 43, "model": None, "reasoningEffort": None},
            )
            text = session["context"]
            reference = civilization_reference("France")
            assert text.count(reference) == 1
            assert text.index(strategy_reference()) < text.index(reference) < text.index("Current game information")
            assert factory.calls == []
            assert await interactive_session.close_session(service, {"jobId": session["jobId"]}) == {"closed": True}
        finally:
            await service.close()

    asyncio.run(run())
