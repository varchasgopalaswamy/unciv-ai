"""Mixed-provider assignment, durable credentials, and model capability checks."""

import asyncio
import json
import stat
from unittest.mock import Mock

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from civ_agent import container_supervisor as supervisor
from civ_agent import providers
from civ_agent.benchmark import generate_runs
from civ_agent.commands import game, runtime
from civ_agent.container_supervisor import agent_auth_arguments
from civ_agent.game_config import GameConfig, ResolvedGameConfig
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
    assert providers.read_api_key(key) == "synthetic-secret"
    assert stat.S_IMODE(key.stat().st_mode) == 0o600
    assert not (tmp_path / "codex").exists()
    assert "synthetic-secret" not in capsys.readouterr().out
    original = {
        "OPENAI_API_KEY": "must-not-leak",
        "CODEX_API_KEY": "also-private",
        "OPENROUTER_API_KEY": "old-value",
        "CIV_OPENROUTER_KEY_FILE": str(key),
    }
    router = providers.provider_environment(original, "openrouter")
    assert router == {"OPENROUTER_API_KEY": "synthetic-secret"}
    assert "OPENROUTER_API_KEY" not in providers.provider_environment(original, "openai")
    for provider in providers.PROVIDERS:
        args = " ".join(agent_auth_arguments(tmp_path, "Rome", provider))
        assert "synthetic-secret" not in args
        assert ("openrouter.key" in args) == (provider == "openrouter")
        assert ("auth-ipc" in args) == (provider == "openai")
        assert ":ro" in args


def test_model_catalog_validates_tools_effort_and_caches_for_resume(tmp_path, monkeypatch):
    async def run():
        fetches = []

        async def catalog(request):
            assert "Authorization" not in request.headers
            fetches.append(True)
            return web.json_response(
                {
                    "data": [
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
                }
            )

        app = web.Application()
        app.router.add_get("/models", catalog)
        async with TestServer(app) as server:
            monkeypatch.setattr(providers, "OPENROUTER_URL", str(server.make_url("/")).rstrip("/"))
            args = await providers.openrouter_model_arguments("test/reasoner", "high", tmp_path)
            assert "model_context_window=32000" in args
            assert "model_supports_reasoning_summaries=true" in args
            assert await providers.openrouter_model_arguments("test/reasoner", "high", tmp_path) == args
            assert len(fetches) == 1
            for model, effort, error in (
                ("test/reasoner", "ultra", "effort"),
                ("test/plain", "high", "reasoning"),
                ("test/no-tools", "none", "tool calling"),
                ("test/missing", "high", "absent"),
                ("test/no-schema", "none", "structured outputs"),
            ):
                with pytest.raises(ValueError, match=error):
                    await providers.openrouter_model_arguments(model, effort, tmp_path)
            args = await providers.openrouter_model_arguments("test/plain", "none", tmp_path)
            assert "model_supports_reasoning_summaries=false" in args
        # Frozen capabilities remain usable when the public endpoint is offline.
        assert "model_context_window=32000" in await providers.openrouter_model_arguments(
            "test/reasoner", "high", tmp_path
        )

    asyncio.run(run())


def test_openrouter_missing_key_does_not_fall_back_to_openai():
    with pytest.raises(ValueError, match="OpenRouter API key missing"):
        providers.provider_environment({"OPENAI_API_KEY": "not-the-right-key"}, "openrouter")


def test_openrouter_only_launch_does_not_require_or_expose_openai_login(tmp_path, monkeypatch):
    root, auth = tmp_path / "match", tmp_path / "auth"
    config = GameConfig(agents={"provider": "openrouter", "model": "test/reasoner"}).resolved()
    write_private_json(root / "resolved.json", config.model_dump())
    record = supervisor.identity(root)
    record.update(auth_root=str(auth), images={"runtime": "test-image"})
    write_private_json(root / "container.json", record)
    write_private(auth / "openrouter.key", b"synthetic-secret\n")
    login, authorize = Mock(), Mock()
    monkeypatch.setattr(supervisor, "ensure_auth", login)
    monkeypatch.setattr(supervisor, "authorize_agent", authorize)
    monkeypatch.setattr(supervisor, "cleanup", lambda *_: None)
    monkeypatch.setattr(supervisor, "create_network", lambda *_, **kwargs: {"subnets": [{"subnet": "10.1.0.0/24"}]})
    monkeypatch.setattr(supervisor, "firewall", lambda *_: None)
    monkeypatch.setattr(supervisor.GameRun, "await_ready", lambda *_: None)
    launched = []
    monkeypatch.setattr(
        supervisor.GameRun, "start", lambda self, args, name, image, *entry: launched.append((args, entry))
    )
    supervisor.GameRun(root).setup()
    login.assert_not_called()
    authorize.assert_not_called()
    assert len(launched) == 4
    assert "openrouter.key" not in " ".join(launched[0][0])
    assert all("CIV_OPENROUTER_KEY_FILE=/run/secrets/openrouter.key" in args for args, _ in launched[1:])
    assert not any("CIV_AUTH_" in arg or "synthetic-secret" in arg for args, _ in launched for arg in args)
    record["auth_root"] = str(root / "credentials")
    write_private_json(root / "container.json", record)
    with pytest.raises(supervisor.ContainerError, match="outside the match"):
        supervisor.GameRun(root)
