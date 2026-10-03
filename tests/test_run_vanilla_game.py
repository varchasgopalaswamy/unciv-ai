"""TOML launches, frozen benchmark settings, and durable restart identity."""

import json
from unittest.mock import Mock

import pytest

from civ_agent import cli
from civ_agent.commands import game
from civ_agent.container_support import ContainerError, identity, locked
from civ_agent.game_config import GameConfig, expand_batch, load_config


def options(root, *flags, action="start"):
    return game.parser(action=action).parse_args(["--state-dir", str(root), *flags])


def test_toml_cli_precedence_and_human_seat(tmp_path):
    source = tmp_path / "input.toml"
    source.write_text("""
[game]
seed = 41
map_size = "Large"
speed = "Standard"
[agents]
model = "base-model"
reasoning_effort = "high"
fast = true
[[players]]
civilization = "Rome"
controller = "agent"
model = "roman-model"
[[players]]
civilization = "Greece"
controller = "agent"
[[players]]
civilization = "France"
controller = "human"
[[players]]
civilization = "Egypt"
controller = "built_in_ai"
""")
    config = game.resolve_options(
        options(
            tmp_path / "game",
            "--config",
            str(source),
            "--seed",
            "42",
            "--map-radius",
            "26",
            "--no-fast",
            "--agent-model",
            "Greece=greek-model",
        )
    )
    assert config.game.seed == 42 and config.game.speed == "Standard"
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
        '[[players]]\ncivilization="Rome"\n[[players]]\ncivilization="Rome"',
        '[[players]]\ncivilization="France"\ncontroller="human"\nmodel="a"',
        '[[players]]\ncivilization="Rome"\n[[players]]\ncivilization="Greece"\ncontroller="human"\n[[players]]\ncivilization="France"\ncontroller="human"',
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


def test_rotations_preserve_native_setup_and_move_model_effort_together():
    config = GameConfig.model_validate(
        {
            "game": {"seed": 45},
            "players": [
                {"civilization": "Rome", "model": "a", "reasoning_effort": "medium"},
                {"civilization": "Greece", "model": "b", "reasoning_effort": "max"},
                {"civilization": "France", "controller": "human"},
                {"civilization": "Egypt", "controller": "built_in_ai"},
            ],
            "batch": {"seeds": [17, 29], "repetitions": 2, "rotate_models": True},
        }
    )
    runs = expand_batch(config)
    assert len(runs) == 8
    for first, second in zip(runs[::2], runs[1::2], strict=True):
        a, b = [GameConfig.model_validate(row["config"]) for row in (first, second)]
        assert a.engine_arguments() == b.engine_arguments()
        assert a.human == b.human == "France"
        assert [(p.model, p.reasoning_effort) for p in b.agent_players] == [("b", "max"), ("a", "medium")]
    same = GameConfig(batch={"rotate_models": True})
    assert len(expand_batch(same)) == 1


def test_saved_config_and_images_survive_input_edits_and_relocation(tmp_path, monkeypatch):
    root = tmp_path / "original"
    source = tmp_path / "game.toml"
    source.write_text('[game]\nseed=19\n[agents]\nmodel="original-model"')
    config = load_config(source).resolved()
    monkeypatch.setattr(game, "resolve_image", lambda _: "sha256:fixed")
    monkeypatch.setattr(game, "validate_setup", lambda *_: {"maxTurns": 500})
    record = game.initialize(root, config, tmp_path / "auth", original=source)
    assert config.limits.max_turns == 501
    source.write_text('[agents]\nmodel="changed-model"')
    moved = tmp_path / "moved"
    root.rename(moved)
    restored = game.resume_configuration(moved, options(moved, action="resume"))
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
    assert cli.main(["game", "start", "--state-dir", str(tmp_path / "match"), "--seed", "4"]) == 0
    background.assert_called_once_with(tmp_path / "match", "game")
    saved = json.loads((tmp_path / "match/resolved.json").read_text())
    assert saved["game"]["seed"] == 4 and saved["limits"]["max_turns"] == 331


def test_interrupted_batch_initialization_resumes_frozen_queue(tmp_path, monkeypatch):
    from civ_agent.commands import batch

    source = tmp_path / "batch.toml"
    source.write_text("[batch]\nseeds=[42,43]\nparallel=2")
    root = tmp_path / "batch"
    monkeypatch.setattr(batch, "preflight", lambda: None)
    monkeypatch.setattr(batch, "resolve_image", lambda _: "pinned-image")
    monkeypatch.setattr(batch, "validate_setup", lambda *_: {"maxTurns": 330})
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
    assert cli.main(["batch", "start", "--config", str(source), "--state-dir", str(root)]) == 2
    background.assert_not_called()
    first_id = identity(root / "games/g0001")["id"]
    assert len(json.loads((root / "batch.json").read_text())["runs"]) == 2
    source.write_text("[batch]\nseeds=[999]")
    monkeypatch.setattr(batch, "initialize", original)
    assert cli.main(["batch", "resume", "--state-dir", str(root)]) == 0
    assert identity(root / "games/g0001")["id"] == first_id
    assert json.loads((root / "games/g0002/resolved.json").read_text())["game"]["seed"] == 43
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
