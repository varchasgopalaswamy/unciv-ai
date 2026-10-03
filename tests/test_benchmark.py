"""Independent seeded games, override precedence and resumable input generation."""

import itertools
import json
import random
import tomllib
from unittest.mock import Mock

import pytest

from civ_agent import cli
from civ_agent.benchmark import generate_runs, seeded_input
from civ_agent.commands import batch, game
from civ_agent.game_config import SEED_FIELDS, GameConfig, ResolvedGameConfig, derived_seed, load_config


@pytest.mark.parametrize("specified", list(itertools.product((False, True), repeat=3)))
def test_each_seed_can_be_overridden_without_affecting_other_derivations(specified):
    overrides = {field: -17 for field, present in zip(SEED_FIELDS, specified, strict=True) if present}
    config = GameConfig(game={"master_seed": 0, "randomize": True, **overrides})
    first = config.resolved()
    random.seed(123)  # Neither the master nor matching uses ambient random state.
    assert first == config.resolved()
    for name in SEED_FIELDS:
        assert getattr(first.game, name) == (overrides[name] if name in overrides else derived_seed(0, name))
    changed = config.model_copy(deep=True)
    changed.game.master_seed = 51
    other = changed.resolved()
    for name in SEED_FIELDS:
        assert (getattr(first.game, name) == getattr(other.game, name)) == (name in overrides)
    assert config.game.master_seed == 0
    assert all(getattr(config.game, name) is None for name in SEED_FIELDS if name not in overrides)


def test_omitted_master_is_saved_and_resolving_again_never_reseeds(monkeypatch):
    choose = Mock(return_value=9223372036854775807)
    monkeypatch.setattr("civ_agent.game_config.secrets.randbits", choose)
    resolved = GameConfig(game={"starting_position_seed": 0, "assignment_seed": 0}).resolved()
    assert resolved.game.master_seed == 9223372036854775807
    assert resolved.game.map_seed == derived_seed(resolved.game.master_seed, "map_seed")
    assert resolved.game.starting_position_seed == resolved.game.assignment_seed == 0
    assert resolved == resolved.resolved()
    assert resolved == ResolvedGameConfig.model_validate_json(resolved.model_dump_json())
    choose.assert_called_once_with(63)


def test_new_format_rejects_old_seed_and_rotation_fields():
    for data in ({"game": {"seed": 42}}, {"batch": {"rotate_players": True}}, {"game": {"map_seed": 0}}):
        with pytest.raises(ValueError):
            GameConfig.model_validate(data)
    for field in ("master_seed", *SEED_FIELDS):
        for invalid in (2**63, -(2**63) - 1):
            with pytest.raises(ValueError):
                GameConfig(game={field: invalid})


def test_twenty_games_are_twenty_independent_runs_with_native_ai_last():
    civilizations = ["Inca", "Siam", "Egypt", "France", "Greece", "Arabia", "China", "Persia"]
    players = [
        {"name": "Unciv", "controller": "built_in_ai"},
        {"name": "Human", "controller": "human"},
        *[{"name": f"Model {i}", "model": "same-model"} for i in range(5)],
        {"name": "Max", "model": "other-model", "reasoning_effort": "max"},
    ]
    config = GameConfig(game={"civilizations": civilizations, "randomize": True}, players=players)
    rows = generate_runs(config, 20, 42)
    assert len(rows) == 20
    assert rows == generate_runs(config, 20, 42)
    assert rows[:5] == generate_runs(config, 5, 42)
    assert {row["config"]["game"]["master_seed"] for row in rows} == set(range(42, 62))
    for name in SEED_FIELDS:
        assert len({row["config"]["game"][name] for row in rows}) == 20
    assert len({tuple(a["player"] for a in row["config"]["assignments"]) for row in rows}) > 1
    for row in rows:
        resolved = ResolvedGameConfig.model_validate(row["config"])
        assert resolved.players == config.players
        assert [p.controller for p in resolved.turn_order] == ["human", *(["agent"] * 6), "built_in_ai"]
        maximum = next(p for p in resolved.assigned_players if p.name == "Max")
        assert (maximum.model, maximum.reasoning_effort) == ("other-model", "max")


def test_batch_uses_template_overrides_and_master_unless_cli_overrides_it():
    config = GameConfig(game={"master_seed": 100, "map_seed": 42, "assignment_seed": 7, "randomize": True})
    rows = generate_runs(config, 3)
    assert [r["config"]["game"]["master_seed"] for r in rows] == [100, 101, 102]
    assert all(r["config"]["game"]["map_seed"] == 42 for r in rows)
    assert all(r["config"]["assignments"] == rows[0]["config"]["assignments"] for r in rows)
    assert len({r["config"]["game"]["starting_position_seed"] for r in rows}) == 3
    assert generate_runs(config, 1, 0)[0]["config"]["game"]["master_seed"] == 0
    boundary = generate_runs(config, 2, 2**63 - 1)
    assert [r["config"]["game"]["master_seed"] for r in boundary] == [2**63 - 1, -(2**63)]
    for count in (0, -1):
        with pytest.raises(ValueError):
            generate_runs(config, count)


def test_cli_independent_seed_overrides_reach_engine(tmp_path):
    source = tmp_path / "input.toml"
    source.write_text("[game]\nmaster_seed=42\nmap_seed=13\nstarting_position_seed=17\nassignment_seed=19\n")
    args = game.parser().parse_args(
        [
            "--config",
            str(source),
            "--master-seed",
            "99",
            "--map-seed",
            "101",
            "--starting-position-seed",
            "0",
            "--assignment-seed",
            "0",
        ]
    )
    config = game.resolve_options(args)
    assert config.game.master_seed == 99
    assert config.game.map_seed == 101
    assert config.game.starting_position_seed == config.game.assignment_seed == 0
    engine = config.engine_arguments()
    assert engine[engine.index("--seed") + 1] == "101"
    assert engine[engine.index("--starting-position-seed") + 1] == "0"


@pytest.mark.parametrize(
    "template", ["# Defaults\n", "[game]\nrandomize=true # Assign all players\nmap_seed=42\n[agents]\nfast=true\n"]
)
def test_seeded_toml_is_a_standalone_game_input_and_preserves_template(template, tmp_path):
    config = GameConfig.model_validate(tomllib.loads(template)).resolved()
    text = seeded_input(template, config)
    source = tmp_path / "input.toml"
    source.write_text(text)
    assert "# Defaults" in text or "# Assign all players" in text
    assert load_config(source).resolved() == config
    assert "assignments" not in tomllib.loads(text)
    assert "batch" not in tomllib.loads(text)


def test_generate_is_offline_and_start_reads_inputs_then_resume_uses_frozen_config(tmp_path, monkeypatch):
    source = tmp_path / "game.toml"
    source.write_text("[game]\nmaster_seed=42\nrandomize=true\n")
    root = tmp_path / "batch"
    preflight = Mock()
    background = Mock(return_value="batch.service")
    monkeypatch.setattr(batch, "preflight", preflight)
    monkeypatch.setattr(batch, "launch_background", background)
    monkeypatch.setattr(batch, "resolve_image", lambda _: "pinned")
    monkeypatch.setattr(game, "validate_setup", lambda *_: {"maxTurns": 330})
    assert cli.main(["batch", "generate", "--config", str(source), "--games", "3", "--state-dir", str(root)]) == 0
    preflight.assert_not_called()
    background.assert_not_called()
    assert not (root / "container.json").exists()
    manifest = json.loads((root / "batch.json").read_text())
    for row in manifest["runs"]:
        config = load_config(root / "games" / row["id"] / "input.toml").resolved()
        assert config.model_dump() == row["config"]
    selected = root / "games/g0002/input.toml"
    selected.write_text(selected.read_text() + '\n[agents]\nmodel="edited-model"\n')
    # Complete an interrupted input write using the saved master, without the source.
    (root / "games/g0003/input.toml").unlink()
    source.unlink()
    assert cli.main(["batch", "start", "--state-dir", str(root), "--parallel", "2"]) == 0
    background.assert_called_once_with(root, "batch")
    saved = json.loads((root / "games/g0002/resolved.json").read_text())
    assert saved["agents"]["model"] == "edited-model"
    assert load_config(root / "games/g0003/input.toml").game.master_seed == 44
    # Resume requires neither templates nor generated inputs and never resamples.
    (root / "input.toml").unlink()
    selected.unlink()
    assert cli.main(["batch", "resume", "--state-dir", str(root)]) == 0
    assert json.loads((root / "games/g0002/resolved.json").read_text()) == saved


def test_generate_dry_run_has_no_files_or_services(tmp_path, monkeypatch, capsys):
    source = tmp_path / "game.toml"
    source.write_text("[game]\nmaster_seed=42\nrandomize=true")
    root = tmp_path / "batch"
    preflight = Mock()
    monkeypatch.setattr(batch, "preflight", preflight)
    assert (
        cli.main(["batch", "start", "--config", str(source), "--games", "20", "--state-dir", str(root), "--dry-run"])
        == 0
    )
    assert len(json.loads(capsys.readouterr().out)["runs"]) == 20
    assert not root.exists()
    preflight.assert_not_called()
