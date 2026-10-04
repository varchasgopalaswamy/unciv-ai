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
        )
    )
    player = next(p for p in resolved.assigned_players if p.name == "Agent 1")
    assert player.civilization != "Rome"
    assert resolved.settings(player)["model"] == "chosen-model"
    assert resolved.settings(player)["reasoning_effort"] == "max"
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
    result = json.loads((tmp_path / "results.json").read_text())["runs"][0]
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
    from civ_agent.tls import write_private_json
    from civ_agent.turn_archive import civilization_directory_name

    config = GameConfig(game={"map_seed": 42, "randomize": True, "assignment_seed": 7}).resolved()
    write_private_json(tmp_path / "resolved.json", config.model_dump())
    arguments = watch_mounts(tmp_path)
    assert arguments[:2] == mount(tmp_path, "/match")  # The overall game remains read-only.
    for player in config.assigned_players:
        directory = tmp_path / "agent-services" / civilization_directory_name(player.civilization)
        for name in ("civilization", "telemetry"):
            assert (directory / name).exists() == (player.controller == "agent")
    assert len(arguments) == 2 * (1 + 2 + 2 * len(config.agent_players))


def test_saved_config_and_images_survive_input_edits_and_relocation(tmp_path, monkeypatch):
    root = tmp_path / "original"
    source = tmp_path / "game.toml"
    source.write_text('[game]\nmap_seed=19\nrandomize=true\n[agents]\nmodel="original-model"')
    config = load_config(source).resolved()
    monkeypatch.setattr(game, "resolve_image", lambda _: "sha256:fixed")
    monkeypatch.setattr(game, "validate_setup", lambda *_: {"maxTurns": 500})
    record = game.initialize(root, config, tmp_path / "auth", original=source)
    assert config.limits.max_turns == 501
    source.write_text('[agents]\nmodel="changed-model"')
    moved = tmp_path / "moved"
    root.rename(moved)
    restored = game.resume_configuration(moved, options(moved, action="resume"))
    assert restored.assignments == config.assignments
    assert restored.game.assignment_seed == config.game.assignment_seed
    assert restored.agents.model == "original-model"
    assert identity(moved)["id"] == record["id"]
    assert identity(moved)["images"]["runtime"] == "sha256:fixed"
    with pytest.raises(ValueError, match="decrease"):
        game.resume_configuration(moved, options(moved, "--max-turns", "3", action="resume"))
    with pytest.raises(ContainerError, match="saved game"):
        game.initialize(moved, restored, tmp_path / "auth")


def test_supervisor_lock_prevents_second_owner(tmp_path):
    with locked(tmp_path / "match/.supervisor.lock"):
        with pytest.raises(ContainerError, match="Another supervisor"):
            with locked(tmp_path / "match/.supervisor.lock"):
                pytest.fail("Two supervisors acquired one game")


def test_launch_is_detached_and_never_runs_scheduler_on_host(tmp_path, monkeypatch):
    monkeypatch.setattr(game, "preflight", lambda: None)
    monkeypatch.setattr(game, "resolve_image", lambda _: "fixed-image")
    monkeypatch.setattr(game, "validate_setup", lambda *_: {"maxTurns": 330})
    background = Mock(return_value="test.service")
    monkeypatch.setattr(game, "launch_background", background)
    assert cli.main(["game", "start", "--state-dir", str(tmp_path / "match"), "--map-seed", "4"]) == 0
    background.assert_called_once_with(tmp_path / "match", "game")
    saved = json.loads((tmp_path / "match/resolved.json").read_text())
    assert saved["game"]["map_seed"] == 4 and saved["limits"]["max_turns"] == 331


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
    from civ_agent.container_supervisor import GameRun
    from civ_agent.tls import write_private_json

    config = GameConfig().resolved()
    game.initialize(
        tmp_path / "game", config, tmp_path / "auth", images={"runtime": "fixed"}, validation={"maxTurns": 330}
    )
    root = tmp_path / "game"
    write_private_json(
        root / "runtime.json",
        {"phase": "stopped", "runner_status": "budget_exhausted", "reason": "Game-turn budget exhausted."},
    )
    run = GameRun(root)
    monkeypatch.setattr(run, "setup", lambda: None)
    monkeypatch.setattr(run, "stop", lambda: None)
    monkeypatch.setattr(run.stop_event, "wait", lambda _: False)
    assert run.run() == "budget_exhausted"
    assert json.loads((root / "container-status.json").read_text())["status"] == "budget_exhausted"


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
