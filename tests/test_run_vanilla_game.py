"""TOML launches, frozen benchmark settings, and durable restart identity."""

import json
from unittest.mock import Mock

import pytest

from civ_agent import cli
from civ_agent.benchmark import generate_runs
from civ_agent.commands import game
from civ_agent.container_support import ContainerError, identity, locked
from civ_agent.game_config import GameConfig, ResolvedGameConfig, load_config


def options(root, *flags, action="start"):
    return game.parser(action=action).parse_args(["--state-dir", str(root), *flags])


def test_economy_review_interval_defaults_and_cli_override(tmp_path):
    assert GameConfig().agents.economy_interval == 5
    config = game.resolve_options(options(tmp_path, "--economy-interval", "3"))
    assert config.agents.economy_interval == 3
    assert all(config.settings(player)["economy_interval"] == 3 for player in config.agent_players)
    with pytest.raises(ValueError):
        game.resolve_options(options(tmp_path, "--economy-interval", "0"))


def test_military_planning_interval_defaults_and_cli_override(tmp_path):
    assert GameConfig().agents.military_interval == 5
    config = game.resolve_options(options(tmp_path, "--military-interval", "3"))
    assert config.agents.military_interval == 3
    assert all(config.settings(player)["military_interval"] == 3 for player in config.agent_players)
    with pytest.raises(ValueError):
        game.resolve_options(options(tmp_path, "--military-interval", "0"))


def test_toml_cli_precedence_and_human_seat(tmp_path):
    source = tmp_path / "input.toml"
    source.write_text("""
[game]
map_seed = 41
civilizations = ["Rome", "Greece", "France", "Egypt"]
map_size = "Large"
speed = "Standard"
[agents]
model = "base-model"
reasoning_effort = "high"
fast = true
[[players]]
name = "First"
controller = "agent"
model = "roman-model"
[[players]]
name = "Second"
controller = "agent"
[[players]]
name = "Human"
controller = "human"
[[players]]
name = "Unciv"
controller = "built_in_ai"
""")
    config = game.resolve_options(
        options(
            tmp_path / "game",
            "--config",
            str(source),
            "--map-seed",
            "42",
            "--map-radius",
            "26",
            "--no-fast",
            "--player-model",
            "Second=greek-model",
        )
    )
    assert config.game.map_seed == 42 and config.game.speed == "Standard"
    assert config.game.map_size is None and config.game.map_radius == 26
    assert not config.agents.fast and config.human == "France"
    assert [config.settings(p)["model"] for p in config.agent_players] == ["roman-model", "greek-model"]
    assert [config.settings(p)["communication_reasoning_effort"] for p in config.agent_players] == [
        "medium",
        "medium",
    ]
    args = config.engine_arguments()
    assert args[args.index("--agent-player") + 1] == "France"
    assert "--human" not in args  # Engine validation has the same seats without opening a display.


@pytest.mark.parametrize(
    "source",
    [
        '[agents]\nreasonning_effort="high"',
        '[game]\nmap_size="Small"\nmap_radius=10',
        "[limits]\nturn_timeout=0",
        '[game]\ncivilizations=["Rome","Greece"]\n[[players]]\nname="Same"\n[[players]]\nname="Same"',
        '[[players]]\nname="Human"\ncontroller="human"\nmodel="a"',
        '[game]\ncivilizations=["Rome","Greece","France"]\n[[players]]\nname="Agent"\n[[players]]\nname="Human1"\ncontroller="human"\n[[players]]\nname="Human2"\ncontroller="human"',
        '[game]\ncivilizations=["Rome","Rome","France","Egypt"]',
        '[game]\ncivilizations=["Rome"]',
        '[[players]]\nname="Unciv"\ncontroller="built_in_ai"\nreasoning_effort="high"',
        "[batch]\nrotate_models=true",
    ],
)
def test_invalid_config_does_not_start_services(tmp_path, source, monkeypatch):
    path = tmp_path / "bad.toml"
    path.write_text(source)
    preflight = Mock()
    monkeypatch.setattr(game, "preflight", preflight)
    assert game.run(options(tmp_path / "match", "--config", str(path))) == 2
    preflight.assert_not_called()
    assert not (tmp_path / "match").exists()


def test_assignment_precedes_turn_order_including_multiple_native_players():
    config = GameConfig.model_validate(
        {
            "game": {"civilizations": ["Rome", "Greece", "France", "Egypt", "China"]},
            "players": [
                {"name": "AI1", "controller": "built_in_ai"},
                {"name": "Human", "controller": "human"},
                {"name": "Agent1"},
                {"name": "AI2", "controller": "built_in_ai"},
                {"name": "Agent2"},
            ],
        }
    ).resolved()
    assert [p.civilization for p in config.assigned_players] == ["Rome", "Greece", "France", "Egypt", "China"]
    assert [(p.name, p.civilization) for p in config.turn_order] == [
        ("Human", "Greece"),
        ("Agent1", "France"),
        ("Agent2", "China"),
        ("AI1", "Rome"),
        ("AI2", "Egypt"),
    ]
    arguments = config.engine_arguments()
    native_players = [
        (flag, value)
        for flag, value in zip(arguments[::2], arguments[1::2], strict=True)
        if flag in {"--agent-player", "--built-in-ai"}
    ]
    assert native_players == [
        ("--agent-player", "Greece"),
        ("--agent-player", "France"),
        ("--agent-player", "China"),
        ("--built-in-ai", "Rome"),
        ("--built-in-ai", "Egypt"),
    ]


def test_assignment_randomness_is_independent_of_map_seed_and_global_rng():
    import random

    config = GameConfig(game={"map_seed": 42, "assignment_seed": 7, "randomize": True})
    first = config.resolved()
    random.seed(123)
    config.game.map_seed = 99
    second = config.resolved()
    assert first.assignments == second.assignments
    assert first.game.map_seed != second.game.map_seed
    assert [a.player for a in first.assignments] != [p.name for p in config.players]
    config.game.randomize = False
    assert [a.player for a in config.resolved().assignments] == [p.name for p in config.players]


def test_player_assignment_and_civilization_placement_seeds_are_independent():
    config = GameConfig(
        game={"master_seed": 42, "assignment_seed": 7, "randomize": True},
        players=[
            {"name": "Human", "controller": "human"},
            {"name": "Agent 1"},
            {"name": "Agent 2"},
            {"name": "Native", "controller": "built_in_ai"},
        ],
    )
    first = config.resolved()
    config.game.assignment_seed = 8
    reassigned = config.resolved()
    assert first.assignments != reassigned.assignments
    assert first.human != reassigned.human
    assert first.turn_order != reassigned.turn_order
    assert first.game.map_seed == reassigned.game.map_seed
    assert first.game.starting_position_seed == reassigned.game.starting_position_seed

    config.game.starting_position_seed = 99
    repositioned = config.resolved()
    assert repositioned.assignments == reassigned.assignments
    assert repositioned.game.map_seed == reassigned.game.map_seed
    assert repositioned.game.starting_position_seed != reassigned.game.starting_position_seed
    expected = reassigned.engine_arguments()
    expected[expected.index("--starting-position-seed") + 1] = "99"
    assert repositioned.engine_arguments() == expected


def test_resolved_assignments_require_every_player_once():
    saved = GameConfig().resolved().model_dump()
    saved["assignments"][0]["player"] = saved["assignments"][1]["player"]
    with pytest.raises(ValueError, match="every player exactly once"):
        ResolvedGameConfig.model_validate(saved)


def test_cli_player_overrides_follow_identity_through_random_assignment(tmp_path):
    resolved = game.resolve_options(
        options(
            tmp_path,
            "--randomize",
            "--assignment-seed",
            "7",
            "--player-model",
            "Agent 1=chosen-model",
            "--player-reasoning-effort",
            "Agent 1=max",
            "--player-communication-reasoning-effort",
            "Agent 1=low",
        )
    )
    player = next(p for p in resolved.assigned_players if p.name == "Agent 1")
    assert player.civilization != "Rome"
    assert resolved.settings(player)["model"] == "chosen-model"
    assert resolved.settings(player)["reasoning_effort"] == "max"
    assert resolved.settings(player)["communication_reasoning_effort"] == "low"
    with pytest.raises(ValueError, match="not an agent player"):
        game.resolve_options(options(tmp_path, "--player-model", "Unciv=chosen-model"))


def test_batch_results_and_status_attribute_civilizations_to_named_players(tmp_path, capsys):
    import csv

    from civ_agent.container_supervisor import batch_summary
    from civ_agent.tls import write_private_json

    runs = generate_runs(
        GameConfig(
            game={"map_seed": 42, "assignment_seed": 7, "randomize": True},
            agents={"model": "default-model", "reasoning_effort": "high"},
        ),
        4,
        100,
    )
    for row in runs:
        write_private_json(tmp_path / "games" / row["id"] / "resolved.json", row["config"])
    batch_summary(
        tmp_path,
        {"format": 2, "parallel": 1, "runs": [{"id": row["id"], "status": "completed"} for row in runs]},
    )
    summary = json.loads((tmp_path / "results.json").read_text())
    assert isinstance(summary["updated_at"], float)
    result = summary["runs"][0]
    expected = ResolvedGameConfig.model_validate(runs[0]["config"])
    assert result["assignment_seed"] == 7
    assert result["players"] == expected.player_records()
    assert result["players"][-1]["name"] == "Unciv"
    assert result["players"][0]["model"] == "default-model"
    assert result["players"][0]["reasoning_effort"] == "high"
    with (tmp_path / "results.csv").open() as stream:
        csv_rows = list(csv.DictReader(stream))
    assert json.loads(csv_rows[0]["players"]) == result["players"]
    assert game.run(options(tmp_path / "games/g0001", action="status")) == 0
    assert json.loads(capsys.readouterr().out)["players"] == result["players"]


def test_dashboard_mounts_follow_assigned_agent_civilizations(tmp_path):
    from civ_agent.container_support import mount, watch_mounts
    from civ_agent.player_paths import PlayerPaths
    from civ_agent.tls import private_directory, write_private_json

    config = GameConfig(game={"map_seed": 42, "randomize": True, "assignment_seed": 7}).resolved()
    write_private_json(tmp_path / "resolved.json", config.model_dump())
    # Pending games do not gain misleading empty service directories on watch.
    assert watch_mounts(tmp_path) == mount(tmp_path, "/match")
    private_directory(tmp_path / "runner")
    for index, player in enumerate(config.agent_players):
        PlayerPaths(tmp_path, index, player.civilization).prepare()
    arguments = watch_mounts(tmp_path)
    assert arguments[:2] == mount(tmp_path, "/match")
    assert not any(":rw," in argument for argument in arguments)
    assert len(arguments) == 2


def test_single_game_and_batch_previews_accept_phoenix_config(tmp_path, monkeypatch, capsys):
    from civ_agent.commands import batch

    inputs = tmp_path / "inputs"
    inputs.mkdir()
    source = inputs / "game.toml"
    source.write_text('[runtime.tracing]\nlocal=true\nendpoint="http://phoenix:6006/v1/traces"\n')
    monkeypatch.setattr(game, "resolve_image", lambda _: "sha256:fixed")
    monkeypatch.setattr(game, "validate_setup", lambda *_: {"maxTurns": 330})
    assert game.run(options(tmp_path / "match", "--config", str(source), "--dry-run")) == 0
    single = json.loads(capsys.readouterr().out)
    preview = json.loads(json.dumps(batch._preview(batch._resolved_inputs(inputs), 1)))
    expected = {"local": True, "endpoint": "http://phoenix:6006/v1/traces"}
    assert single["config"]["runtime"]["tracing"] == expected
    assert preview["inputs"][0]["config"]["runtime"]["tracing"] == expected
    assert not (tmp_path / "match").exists()


def test_supervisor_lock_prevents_second_owner(tmp_path):
    with locked(tmp_path / "match/.supervisor.lock"):
        with pytest.raises(ContainerError, match="Another supervisor"):
            with locked(tmp_path / "match/.supervisor.lock"):
                pytest.fail("Two supervisors acquired one game")


def test_missing_login_leaves_start_retryable_and_authenticated_launch_detaches(tmp_path, monkeypatch, capsys):
    from civ_agent.tls import write_private_json

    monkeypatch.setattr(game, "preflight", lambda: None)
    monkeypatch.setattr(game, "resolve_image", lambda _: "fixed-image")
    monkeypatch.setattr(game, "validate_setup", lambda *_: {"maxTurns": 330})
    background = Mock(return_value="test.service")
    monkeypatch.setattr(game, "launch_background", background)
    auth = tmp_path / "auth with spaces"
    command = ["game", "start", "--state-dir", str(tmp_path / "match"), "--auth-dir", str(auth), "--map-seed", "4"]
    assert cli.main(command) == 2
    assert f"civ-agent auth login --auth-dir '{auth}'" in capsys.readouterr().err
    assert not auth.exists() and not (tmp_path / "match").exists()
    background.assert_not_called()
    write_private_json(auth / "openai-subscription.json", {"login": "test"})
    assert cli.main(command) == 0
    background.assert_called_once_with(tmp_path / "match", "game")
    saved = json.loads((tmp_path / "match/resolved.json").read_text())
    assert saved["game"]["map_seed"] == 4 and saved["limits"]["max_turns"] == 331


@pytest.mark.parametrize("action", ["start", "resume"])
def test_interactive_game_completes_login_and_displays_browser_password(tmp_path, monkeypatch, action, capsys):
    from civ_agent import container_supervisor
    from civ_agent.tls import write_private, write_private_json

    root, auth = tmp_path / "match", tmp_path / "custom auth"
    if action == "resume":
        game.initialize(root, GameConfig().resolved(), auth, images={"runtime": "image"}, validation={"maxTurns": 40})
    monkeypatch.setattr(game.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(game.sys.stdout, "isatty", lambda: True)
    monkeypatch.setattr(game, "preflight", lambda: None)
    monkeypatch.setattr(game, "resolve_image", lambda _: "image")
    monkeypatch.setattr(game, "validate_setup", lambda *_: {"maxTurns": 40})
    calls = []

    def login(directory, *, only_if_missing):
        assert only_if_missing and directory == auth
        assert root.exists() == (action == "resume")
        calls.append("login")
        write_private_json(auth / "openai-subscription.json", {"login": "test"})

    monkeypatch.setattr(container_supervisor, "login_subscription", login)

    def launch(*_):
        calls.append("launch")
        write_private(root / "desktop/password.txt", b"VncPass1\n")
        write_private_json(
            root / "container-status.json",
            {
                "status": "running",
                "updated_at": len(calls),
                "browser": {"url": "http://127.0.0.1:42753/vnc.html", "password_file": "desktop/password.txt"},
            },
        )
        return "test.service"

    monkeypatch.setattr(game, "launch_background", launch)
    arguments = ["--auth-dir", str(auth)] if action == "start" else []
    assert game.run(options(root, *arguments, action=action)) == 0
    assert calls == ["login", "launch"]
    assert "Play: http://127.0.0.1:42753/vnc.html\nPassword: VncPass1\n" in capsys.readouterr().out
    assert game.run(options(root, action="resume")) == 0
    assert calls == ["login", "launch", "launch"]
    assert "Password: VncPass1\n" in capsys.readouterr().out
    monkeypatch.setattr(game.sys.stdout, "isatty", lambda: False)
    assert game.run(options(root, action="resume")) == 0
    assert "VncPass1" not in capsys.readouterr().out


def test_terminal_reports_new_failure_instead_of_previous_browser(tmp_path, monkeypatch, capsys):
    from civ_agent.tls import write_private_json

    path = tmp_path / "container-status.json"
    previous = {"status": "running", "browser": {"url": "old browser"}}
    write_private_json(path, previous)
    monkeypatch.setattr(
        game.time, "sleep", lambda _: write_private_json(path, {"status": "failed", "reason": "broker unavailable"})
    )
    with pytest.raises(ContainerError, match="broker unavailable"):
        game.show_startup(tmp_path, previous, 5)
    assert "old browser" not in capsys.readouterr().out


def test_interrupted_batch_initialization_resumes_frozen_queue(tmp_path, monkeypatch):
    from civ_agent.commands import batch

    source = tmp_path / "batch.toml"
    source.write_text("[game]\nmaster_seed=42\nrandomize=true")
    inputs = tmp_path / "inputs"
    root = tmp_path / "batch"
    assert (
        cli.main(
            [
                "batch",
                "generate",
                "--config",
                str(source),
                "--games",
                "2",
                "--batch-seed",
                "42",
                "--output",
                str(inputs),
            ]
        )
        == 0
    )
    monkeypatch.setattr(batch, "preflight", lambda: None)
    monkeypatch.setattr(batch, "resolve_image", lambda _: "pinned-image")
    monkeypatch.setattr(game, "validate_setup", lambda *_: {"maxTurns": 330})
    background = Mock(return_value="batch-service")
    monkeypatch.setattr(batch, "launch_background", background)
    original = batch.initialize
    calls = 0

    def interrupted(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("interrupted while preparing second game")
        return original(*args, **kwargs)

    monkeypatch.setattr(batch, "initialize", interrupted)
    assert (
        cli.main(
            [
                "batch",
                "start",
                "--inputs",
                str(inputs),
                "--parallel",
                "2",
                "--state-dir",
                str(root),
            ]
        )
        == 2
    )
    background.assert_not_called()
    assert not root.exists()
    monkeypatch.setattr(batch, "initialize", original)
    assert (
        cli.main(
            [
                "batch",
                "start",
                "--inputs",
                str(inputs),
                "--parallel",
                "2",
                "--state-dir",
                str(root),
            ]
        )
        == 0
    )
    assert identity(root / "games/g0001")["id"] != identity(root / "games/g0002")["id"]
    assert (
        json.loads((root / "games/g0002/resolved.json").read_text())["game"]["master_seed"]
        == load_config(inputs / "g0002.toml").game.master_seed
    )
    background.assert_called_once_with(root, "batch")


def test_budget_exhaustion_is_distinct_from_a_resumable_operator_pause(tmp_path, monkeypatch):
    from civ_agent import container_supervisor as supervisor
    from civ_agent.container_supervisor import GameRun
    from civ_agent.container_support import ContainerService
    from civ_agent.tls import write_private_json

    config = GameConfig().resolved()
    game.initialize(
        tmp_path / "game",
        config,
        tmp_path / "auth",
        images={"runtime": "sha256:" + "b" * 64},
        validation={"maxTurns": 330},
    )
    root = tmp_path / "game"
    run = GameRun(root)
    launch = "123456789abc"
    service = ContainerService(
        run.controller, "a" * 64, "sha256:" + "b" * 64, run.deployment_id, run.controller + "-" + launch + ".service"
    )
    controller = {
        "Id": service.cid,
        "Name": run.controller,
        "Image": service.image,
        "State": {"Running": True, "StartedAt": "2024-01-01T00:00:00Z"},
    }

    def setup():
        run.record["launch"] = launch
        run.names, run.services = [run.controller], [service]
        write_private_json(
            root / "runtime.json",
            {
                "phase": "stopped",
                "launch": launch,
                "runner_status": "budget_exhausted",
                "reason": "Game-turn budget exhausted.",
            },
        )

    def verified(binding):
        assert binding == service
        return controller, {"ActiveState": "active"}

    monkeypatch.setattr(run, "setup", setup)
    stopped = Mock()
    monkeypatch.setattr(run, "stop", stopped)
    monkeypatch.setattr(supervisor, "verify_container_service", verified)
    monkeypatch.setattr(supervisor, "unit_state", lambda unit: {"ActiveState": "active"})
    monkeypatch.setattr(
        supervisor,
        "podman",
        Mock(side_effect=AssertionError("Current terminal budget must settle before stats or lifecycle operations")),
    )
    monkeypatch.setattr(run.stop_event, "wait", lambda _: False)
    assert run.run() == "budget_exhausted"
    assert json.loads((root / "container-status.json").read_text())["status"] == "budget_exhausted"
    stopped.assert_called_once_with()


@pytest.mark.parametrize("reason", ["serverOverloaded", "usageLimitExceeded"])
def test_native_pause_does_not_relaunch_from_untyped_provider_reason(tmp_path, monkeypatch, reason):
    from civ_agent.container_supervisor import GameRun
    from civ_agent.tls import write_private_json
    from tests.test_native_status import native_files

    root = tmp_path / "game"
    game.initialize(
        root, GameConfig().resolved(), tmp_path / "auth", images={"runtime": "fixed"}, validation={"maxTurns": 330}
    )
    _, status, _ = native_files(root)
    write_private_json(
        root / "native-runtime/status.json",
        status.model_copy(update={"mode": "PAUSED", "quiesced": True, "reason": reason}).model_dump(mode="json"),
    )
    run = GameRun(root)
    once = Mock(return_value="paused")
    monkeypatch.setattr(run, "run_once", once)
    assert run.run() == "paused"
    once.assert_called_once()


def test_stop_reports_cleanup_failure_without_retrying_it(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from civ_agent.tls import write_private_json

    identity(tmp_path)
    write_private_json(tmp_path / "resolved.json", GameConfig().resolved().model_dump())
    monkeypatch.setattr(game, "command", lambda *a, **k: SimpleNamespace(returncode=1))
    cleanup = Mock(side_effect=ContainerError("network cleanup failed"))
    monkeypatch.setattr(game, "cleanup", cleanup)
    with pytest.raises(ContainerError, match="network cleanup failed"):
        game.stop_game(tmp_path)
    cleanup.assert_called_once()
