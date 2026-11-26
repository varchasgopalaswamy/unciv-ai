"""Mixed-provider assignment, durable credentials, and model capability checks."""

import asyncio
import json
import stat
from unittest.mock import Mock

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from civ_agent import container_supervisor as supervisor
from civ_agent.benchmark import generate_runs
from civ_agent.commands import game, runtime
from civ_agent.container_supervisor import agent_auth_arguments
from civ_agent.game_config import EconomyModel, GameConfig, ResolvedGameConfig
from civ_agent.orchestration import provider_preparation
from civ_agent.orchestration.contracts import ModelSelection
from civ_agent.orchestration.managed_player import NativePlayerSession, NativePlayerSettings
from civ_agent.orchestration.provider_capabilities import prepare_profile_contract
from civ_agent.orchestration.provider_preparation import fetch_openrouter_catalog, prepare_managed_sessions
from civ_agent.orchestration.providers import ProviderClients
from civ_agent.protocol import JsonObject
from civ_agent.provider_credentials import PROVIDERS, read_api_key
from civ_agent.tls import write_private, write_private_json


def mixed_config():
    return GameConfig(
        game={"master_seed": 1, "randomize": True},
        agents={"fast": True},
        players=[
            {"name": "OpenAI"},
            {"name": "Router", "provider": "openrouter", "model": "test/reasoner", "reasoning_effort": "high"},
            {"name": "Human", "controller": "human"},
            {"name": "Native", "controller": "built_in_ai"},
        ],
    )


def test_provider_follows_player_through_random_assignment_and_resume():
    for row in generate_runs(mixed_config(), 8):
        config = ResolvedGameConfig.model_validate_json(json.dumps(row["config"]))
        players = {p.name: config.settings(p) for p in config.agent_players}
        assert players["OpenAI"]["provider"] == "openai" and players["OpenAI"]["fast"]
        assert players["Router"]["provider"] == "openrouter" and not players["Router"]["fast"]
        assert players["Router"]["model"] == "test/reasoner" and players["Router"]["reasoning_effort"] == "high"
        assert config.turn_order[-1].controller == "built_in_ai"
        assert {r["name"]: r["provider"] for r in config.player_records() if r["controller"] == "agent"} == {
            "OpenAI": "openai",
            "Router": "openrouter",
        }


def test_cli_provider_override_and_invalid_provider():
    args = game.parser().parse_args(
        ["--player-provider", "Agent 1=openrouter", "--player-model", "Agent 1=test/reasoner"]
    )
    config = game.resolve_options(args)
    assert config.settings(config.agent_players[0])["provider"] == "openrouter"
    all_router = game.parser().parse_args(
        [
            "--provider",
            "openrouter",
            *[arg for index in range(1, 4) for arg in ("--player-model", f"Agent {index}=test/reasoner")],
        ]
    )
    resolved = game.resolve_options(all_router)
    assert all(resolved.settings(p)["provider"] == "openrouter" for p in resolved.agent_players)
    assert all(resolved.settings(p)["model"] == "test/reasoner" for p in resolved.agent_players)
    for data in (
        {"agents": {"provider": "typo"}},
        {"agents": {"provider": "openrouter"}},
        {"players": [{"name": "a", "controller": "human", "provider": "openrouter"}]},
    ):
        with pytest.raises(ValueError):
            GameConfig.model_validate(data)


def test_openrouter_login_is_private_durable_and_needs_no_openai_or_podman(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("OPENROUTER_API_KEY", "synthetic-secret")
    args = runtime.parser(group="auth", action="login").parse_args(
        ["--provider", "openrouter", "--auth-dir", str(tmp_path)]
    )
    assert runtime.run(args) == 0
    key = tmp_path / "openrouter.key"
    assert read_api_key(key) == "synthetic-secret"
    assert stat.S_IMODE(key.stat().st_mode) == 0o600
    assert not (tmp_path / "codex").exists()
    assert "synthetic-secret" not in capsys.readouterr().out
    for provider in PROVIDERS:
        args = " ".join(agent_auth_arguments(tmp_path, "Rome", provider))
        assert "synthetic-secret" not in args
        assert ("openrouter.key" in args) == (provider == "openrouter")
        assert ("auth-ipc" in args) == (provider == "openai")
        assert ":ro" in args


def test_model_catalog_validates_tools_effort_and_caches_for_resume(monkeypatch):
    async def run():
        fetches = []
        rows: list[JsonObject] = [
            {
                "id": "test/reasoner",
                "context_length": 32000,
                "supported_parameters": ["tools", "structured_outputs", "reasoning"],
                "reasoning": {"supported_efforts": ["high", "low"]},
            },
            {
                "id": "test/plain",
                "context_length": 16000,
                "supported_parameters": ["tools", "structured_outputs"],
            },
            {"id": "test/no-schema", "context_length": 16000, "supported_parameters": ["tools"]},
            {"id": "test/no-tools", "context_length": 16000, "supported_parameters": []},
        ]

        async def catalog(request):
            assert "Authorization" not in request.headers
            fetches.append(True)
            return web.json_response({"data": rows})

        settings = NativePlayerSettings(
            provider="openrouter",
            model="test/reasoner",
            reasoning_effort="high",
            communication_reasoning_effort="low",
            economy_model=EconomyModel(provider="openrouter", model="test/reasoner", reasoning_effort="high"),
            military_model=EconomyModel(provider="openrouter", model="test/reasoner", reasoning_effort="high"),
        )
        original = NativePlayerSession(game="game", player="p0", settings=settings)
        app = web.Application()
        app.router.add_get("/models", catalog)
        async with TestServer(app) as server:
            monkeypatch.setattr(provider_preparation, "CATALOG_URL", str(server.make_url("/models")))
            prepared = await prepare_managed_sessions([original])
            contract = prepared[0].get_provider_contract("gameplay")
            assert contract is not None and contract.context_window == 32000
            assert contract.profile["supports_thinking"] is True
            assert await prepare_managed_sessions([original], frozen=prepared) == prepared
            assert len(fetches) == 1
            capabilities = await fetch_openrouter_catalog({row["id"] for row in rows})
            for model, effort, error in (
                ("test/reasoner", "max", "effort"),
                ("test/plain", "high", "reasoning"),
                ("test/no-tools", "none", "tool calling"),
                ("test/no-schema", "none", "structured outputs"),
            ):
                selection = ModelSelection(
                    provider="openrouter", authentication="api_key", model=model, reasoning_effort=effort
                )
                with pytest.raises(ValueError, match=error):
                    await prepare_profile_contract(selection, catalog=capabilities[model])
            with pytest.raises(ValueError, match="WORKFLOW_CATALOG_UNAVAILABLE"):
                await fetch_openrouter_catalog({"test/missing"})
            plain = await prepare_profile_contract(
                ModelSelection(
                    provider="openrouter", authentication="api_key", model="test/plain", reasoning_effort="none"
                ),
                catalog=capabilities["test/plain"],
            )
            assert plain.profile["supports_thinking"] is False
        # Retained native contracts remain usable after the public endpoint closes.
        before = len(fetches)
        assert await prepare_managed_sessions([original], frozen=prepared) == prepared
        assert len(fetches) == before

    asyncio.run(run())


def test_openrouter_missing_key_does_not_fall_back_to_environment_or_another_provider(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "not-the-right-key")
    monkeypatch.setenv("OPENROUTER_API_KEY", "environment-is-not-authority")

    async def run():
        clients = ProviderClients(openai_key_file=tmp_path / "must-not-read")
        try:
            with pytest.raises(ValueError, match="WORKFLOW_API_KEY_REQUIRED"):
                await clients.model(
                    ModelSelection(provider="openrouter", authentication="api_key", model="test/reasoner")
                )
            assert not clients.models
        finally:
            await clients.close()

    asyncio.run(run())


def test_openrouter_only_launch_does_not_require_or_expose_openai_login(tmp_path, monkeypatch):
    root, auth = tmp_path / "match", tmp_path / "auth"
    config = GameConfig(agents={"provider": "openrouter", "model": "test/reasoner"}).resolved()
    write_private_json(root / "resolved.json", config.model_dump())
    record = supervisor.identity(root)
    record.update(auth_root=str(auth), images={"runtime": "sha256:" + "1" * 64})
    write_private_json(root / "container.json", record)
    write_private(auth / "openrouter.key", b"synthetic-secret\n")
    login, authorize = Mock(), Mock()
    monkeypatch.setattr(supervisor, "ensure_auth", login)
    monkeypatch.setattr(supervisor, "authorize_agent", authorize)
    monkeypatch.setattr(supervisor, "cleanup", lambda _: None)
    monkeypatch.setattr(supervisor, "create_network", lambda *_, **__: {"subnets": [{"subnet": "10.55.0.0/24"}]})
    monkeypatch.setattr(supervisor, "firewall", lambda *_: None)
    monkeypatch.setattr(supervisor.GameRun, "await_ready", lambda *_: None)
    run = supervisor.GameRun(root)
    started = []
    monkeypatch.setattr(run, "start", lambda arguments, name, image, *entry: started.append((arguments, name, entry)))
    run.setup()
    login.assert_not_called()
    authorize.assert_not_called()
    assert len(started) == 1
    arguments, name, entry = started[0]
    assert name == run.controller and entry == ("controller",)
    assert str(auth / "openrouter.key") + ":/run/secrets/openrouter.key:ro,z" in arguments
    assert not any(
        "/auth-ipc" in argument or "CIV_AUTH_" in argument or "synthetic-secret" in argument for argument in arguments
    )
    record["auth_root"] = str(root / "credentials")
    write_private_json(root / "container.json", record)
    with pytest.raises(supervisor.ContainerError, match="outside the match"):
        supervisor.GameRun(root)
