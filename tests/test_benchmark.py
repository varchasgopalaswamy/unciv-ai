"""Independent seeded games, override precedence and resumable input generation."""

import itertools
import json
import random
import shutil
import tomllib
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from civ_agent import cli
from civ_agent.benchmark import derive_master_seed, generate_runs, input_plans, seeded_input
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
    assert len({row["config"]["game"]["master_seed"] for row in rows}) == 20
    assert [row["batch_seed"] for row in rows] == [42] * 20
    assert [row["game_index"] for row in rows] == list(range(1, 21))
    for name in SEED_FIELDS:
        assert len({row["config"]["game"][name] for row in rows}) == 20
    assert len({tuple(a["player"] for a in row["config"]["assignments"]) for row in rows}) > 1
    for row in rows:
        resolved = ResolvedGameConfig.model_validate(row["config"])
        assert resolved.players == config.players
        assert [p.controller for p in resolved.turn_order] == ["human", *(["agent"] * 6), "built_in_ai"]
        maximum = next(p for p in resolved.assigned_players if p.name == "Max")
        assert (maximum.model, maximum.reasoning_effort) == ("other-model", "max")


def test_batch_uses_explicit_child_overrides_but_not_template_master():
    config = GameConfig(game={"master_seed": 100, "map_seed": 42, "assignment_seed": 7, "randomize": True})
    rows = generate_runs(config, 3, 123)
    assert [r["config"]["game"]["master_seed"] for r in rows] != [100, 101, 102]
    assert all(r["config"]["game"]["map_seed"] == 42 for r in rows)
    assert all(r["config"]["assignments"] == rows[0]["config"]["assignments"] for r in rows)
    assert len({r["config"]["game"]["starting_position_seed"] for r in rows}) == 3
    assert (
        generate_runs(config, 1, 0)[0]["config"]["game"]["master_seed"]
        == generate_runs(config, 1, 0)[0]["config"]["game"]["master_seed"]
    )
    for count in (0, -1):
        with pytest.raises(ValueError):
            generate_runs(config, count)


def test_batch_master_collision_retry_is_stable():
    first = derive_master_seed(42, 3)
    retry = derive_master_seed(42, 3, {first})
    assert retry != first
    assert retry == derive_master_seed(42, 3, {first})


def test_generated_input_keeps_explicit_children_and_omits_others():
    template = """# Keep this comment
[game]
master_seed = 999 # replaced
map_seed = 13
assignment_seed = 19
randomize = true
"""
    config = GameConfig.model_validate(tomllib.loads(template))
    batch_seed, plans = input_plans(template, config, 2, 42)
    assert batch_seed == 42
    for plan in plans:
        data = tomllib.loads(plan["content"])
        assert data["game"]["map_seed"] == 13
        assert data["game"]["assignment_seed"] == 19
        assert "starting_position_seed" not in data["game"]
        assert f"# Batch seed: {batch_seed}" in plan["content"]
        assert f"# Game index: {plan['game_index']}" in plan["content"]
        assert "# Keep this comment" in plan["content"]
        resolved = GameConfig.model_validate(data).resolved()
        assert resolved.game.master_seed == plan["master_seed"]
        assert resolved.game.starting_position_seed is not None


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
    inputs = tmp_path / "inputs"
    root = tmp_path / "batch"
    preflight = Mock()
    background = Mock(return_value="batch.service")
    monkeypatch.setattr(batch, "preflight", preflight)
    monkeypatch.setattr(batch, "launch_background", background)
    monkeypatch.setattr(batch, "resolve_image", lambda _: "pinned")
    monkeypatch.setattr(game, "validate_setup", lambda *_: {"maxTurns": 330})
    assert (
        cli.main(
            [
                "batch",
                "generate",
                "--config",
                str(source),
                "--games",
                "3",
                "--batch-seed",
                "42",
                "--output",
                str(inputs),
            ]
        )
        == 0
    )
    preflight.assert_not_called()
    background.assert_not_called()
    assert sorted(path.name for path in inputs.iterdir()) == ["g0001.toml", "g0002.toml", "g0003.toml"]
    assert not list(inputs.glob("**/resolved.json"))
    selected = inputs / "g0002.toml"
    selected_text = selected.read_text()
    master_line = next(line for line in selected_text.splitlines() if line.startswith("master_seed"))
    selected.write_text(
        selected_text.replace(master_line, "master_seed = 12345") + '\n[agents]\nmodel="edited-model"\n'
    )
    source.unlink()
    assert (
        cli.main(
            [
                "batch",
                "start",
                "--inputs",
                str(inputs),
                "--state-dir",
                str(root),
                "--parallel",
                "2",
            ]
        )
        == 0
    )
    background.assert_called_once_with(root, "batch")
    manifest = json.loads((root / "batch.json").read_text())
    assert all(set(row) == {"id", "status"} for row in manifest["runs"])
    saved = json.loads((root / "games/g0002/resolved.json").read_text())
    assert saved["game"]["master_seed"] == 12345
    assert saved["agents"]["model"] == "edited-model"
    shutil.rmtree(inputs)
    # Resume requires neither the template nor the original generated inputs.
    assert cli.main(["batch", "resume", "--state-dir", str(root)]) == 0
    assert json.loads((root / "games/g0002/resolved.json").read_text()) == saved


def test_generate_dry_run_has_no_files_or_services(tmp_path, monkeypatch, capsys):
    source = tmp_path / "game.toml"
    source.write_text("[game]\nmaster_seed=42\nrandomize=true")
    root = tmp_path / "batch"
    preflight = Mock()
    monkeypatch.setattr(batch, "preflight", preflight)
    assert (
        cli.main(
            [
                "batch",
                "generate",
                "--config",
                str(source),
                "--games",
                "20",
                "--batch-seed",
                "42",
                "--output",
                str(root),
                "--dry-run",
            ]
        )
        == 0
    )
    preview = json.loads(capsys.readouterr().out)
    assert preview["batch_seed"] == 42 and len(preview["inputs"]) == 20
    assert not root.exists()
    preflight.assert_not_called()


def test_same_input_directory_can_start_independent_saved_runs(tmp_path, monkeypatch):
    source = tmp_path / "game.toml"
    source.write_text("[game]\nrandomize=true\n")
    inputs = tmp_path / "inputs"
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
    monkeypatch.setattr(batch, "resolve_image", lambda _: "pinned")
    monkeypatch.setattr(game, "validate_setup", lambda *_: {"maxTurns": 330})
    monkeypatch.setattr(batch, "launch_background", Mock(return_value="batch.service"))
    first = tmp_path / "run-one"
    second = tmp_path / "run-two"
    for root in (first, second):
        assert (
            cli.main(
                [
                    "batch",
                    "start",
                    "--inputs",
                    str(inputs),
                    "--state-dir",
                    str(root),
                ]
            )
            == 0
        )
    first_config = json.loads((first / "games/g0001/resolved.json").read_text())
    second_config = json.loads((second / "games/g0001/resolved.json").read_text())
    assert first_config == second_config
    assert (
        json.loads((first / "container.json").read_text())["id"]
        != json.loads((second / "container.json").read_text())["id"]
    )


@pytest.fixture
def batch_runtime(monkeypatch):
    runtime = SimpleNamespace(
        preflight=Mock(),
        images=Mock(side_effect=lambda name: f"pinned:{name}"),
        background=Mock(return_value="batch.service"),
        validation=Mock(return_value={"maxTurns": 330}),
    )
    monkeypatch.setattr(batch, "preflight", runtime.preflight)
    monkeypatch.setattr(batch, "resolve_image", runtime.images)
    monkeypatch.setattr(batch, "launch_background", runtime.background)
    monkeypatch.setattr(game, "validate_setup", runtime.validation)
    return runtime


@pytest.mark.parametrize("flag", [None, "--fast", "--no-fast"])
@pytest.mark.parametrize("dry_run", [False, True])
def test_batch_start_priority_override_is_saved_and_previewed(tmp_path, batch_runtime, capsys, flag, dry_run):
    inputs, root = tmp_path / "inputs", tmp_path / "run"
    inputs.mkdir()
    originals = {}
    for index, fast in enumerate((True, False), 1):
        path = inputs / f"g{index:04d}.toml"
        path.write_text(f"[game]\nmaster_seed={index}\n[agents]\nfast={str(fast).lower()}\n")
        originals[path] = path.read_bytes()
    arguments = ["batch", "start", "--inputs", str(inputs), "--state-dir", str(root)]
    if flag:
        arguments.append(flag)
    if dry_run:
        arguments.append("--dry-run")
    assert cli.main(arguments) == 0
    output = capsys.readouterr().out
    configs = (
        [item["config"] for item in json.loads(output)["inputs"]]
        if dry_run
        else [json.loads(path.read_text()) for path in sorted(root.glob("games/*/resolved.json"))]
    )
    expected = [True, False] if flag is None else [flag == "--fast"] * 2
    assert [config["agents"]["fast"] for config in configs] == expected
    for config, fast in zip(configs, expected, strict=True):
        resolved = ResolvedGameConfig.model_validate(config)
        assert all(resolved.settings(player)["fast"] is fast for player in resolved.agent_players)
    assert all(path.read_bytes() == original for path, original in originals.items())
    if dry_run:
        batch_runtime.background.assert_not_called()
        batch_runtime.preflight.assert_not_called()
        assert not root.exists()


@pytest.mark.parametrize("stage", ["after_read", "during_validation"])
@pytest.mark.parametrize("change", ["edit", "delete"])
def test_start_resolves_and_saves_the_same_input_snapshot(tmp_path, monkeypatch, batch_runtime, stage, change):
    inputs = tmp_path / "inputs"
    inputs.mkdir()
    originals = {}
    for index in (1, 2):
        path = inputs / f"g{index:04d}.toml"
        content = (
            f"# Café opening {index}\r\n[game]\r\nmaster_seed={index}\r\nmap_seed=17\r\nrandomize=true\r\n"
            '[agents]\r\nmodel="original-model"\r\n'
        ).encode()
        path.write_bytes(content)
        originals[path] = content

    def change_inputs():
        for path in originals:
            if change == "edit":
                path.write_text('[game]\nmaster_seed=999\n[agents]\nmodel="edited-model"\n')
            else:
                path.unlink()

    if stage == "after_read":
        read_inputs = batch._input_files

        def snapshot(directory):
            files = read_inputs(directory)
            change_inputs()
            return files

        monkeypatch.setattr(batch, "_input_files", snapshot)
    else:

        def validate(*_):
            if batch_runtime.validation.call_count == 1:
                change_inputs()
            return {"maxTurns": 330}

        batch_runtime.validation.side_effect = validate

    root = tmp_path / "run"
    assert (
        cli.main(
            [
                "batch",
                "start",
                "--inputs",
                str(inputs),
                "--state-dir",
                str(root),
                "--auth-dir",
                str(tmp_path / "auth"),
            ]
        )
        == 0
    )
    for path, content in originals.items():
        saved = root / "games" / path.stem
        assert (saved / "input.toml").read_bytes() == content
        expected = GameConfig.model_validate(tomllib.loads(content.decode("utf-8"))).resolved()
        expected.limits.max_turns = 331
        assert json.loads((saved / "resolved.json").read_text()) == expected.model_dump()
    assert batch_runtime.validation.call_count == 2
    batch_runtime.background.assert_called_once_with(root, "batch")


@pytest.fixture
def saved_batch(tmp_path, batch_runtime):
    inputs = tmp_path / "inputs"
    inputs.mkdir()
    template = """[game]
master_seed = 42
civilizations = ["Rome", "Greece"]
[[players]]
name = "Human"
controller = "human"
[[players]]
name = "Agent"
[runtime]
browser_port = 6080
"""
    for name in ("g0001", "g0002"):
        (inputs / f"{name}.toml").write_text(template)
    root = tmp_path / "run"
    assert (
        cli.main(
            [
                "batch",
                "start",
                "--inputs",
                str(inputs),
                "--state-dir",
                str(root),
                "--auth-dir",
                str(tmp_path / "auth"),
            ]
        )
        == 0
    )
    manifest_path = root / "batch.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["runs"][0]["status"] = "paused"
    manifest["runs"][1].update(status="failed", error="Previous game failure")
    manifest_path.write_text(json.dumps(manifest))
    shutil.rmtree(inputs)
    batch_runtime.background.reset_mock()
    batch_runtime.images.reset_mock()
    return root


@pytest.mark.parametrize("port,parallel,result", [(6080, 2, 2), (6080, 1, 0), (0, 2, 0)])
def test_resume_validates_saved_ports_before_changing_state(saved_batch, batch_runtime, capsys, port, parallel, result):
    root = saved_batch
    records = {}
    for game_root in sorted((root / "games").iterdir()):
        path = game_root / "resolved.json"
        config = json.loads(path.read_text())
        config["runtime"]["browser_port"] = port
        path.write_text(json.dumps(config))
        record = game_root / "container.json"
        records[record] = record.read_bytes()
    manifest_path = root / "batch.json"
    original_manifest = manifest_path.read_bytes()
    assert (
        cli.main(
            [
                "batch",
                "resume",
                "--state-dir",
                str(root),
                "--parallel",
                str(parallel),
                "--retry-failed",
                "--image",
                "runtime:new",
                "--desktop-image",
                "desktop:new",
            ]
        )
        == result
    )
    if result:
        assert "Concurrent human games need runtime.browser_port = 0" in capsys.readouterr().err
        assert manifest_path.read_bytes() == original_manifest
        assert all(path.read_bytes() == content for path, content in records.items())
        batch_runtime.images.assert_not_called()
        batch_runtime.background.assert_not_called()
    else:
        manifest = json.loads(manifest_path.read_text())
        assert manifest["parallel"] == parallel
        assert all(run["status"] == "pending" and "error" not in run for run in manifest["runs"])
        assert all(
            json.loads(path.read_text())["images"] == {"runtime": "pinned:runtime:new", "desktop": "pinned:desktop:new"}
            for path in records
        )
        batch_runtime.background.assert_called_once_with(root, "batch")


@pytest.mark.parametrize("damage", ["missing", "invalid_json", "invalid_config"])
def test_resume_validates_all_saved_configs_before_changing_state(saved_batch, batch_runtime, capsys, damage):
    root = saved_batch
    path = root / "games/g0002/resolved.json"
    if damage == "missing":
        path.unlink()
    else:
        path.write_text("{" if damage == "invalid_json" else "{}")
    manifest_path = root / "batch.json"
    original_manifest = manifest_path.read_bytes()
    records = {path: path.read_bytes() for path in root.glob("games/*/container.json")}
    assert (
        cli.main(
            [
                "batch",
                "resume",
                "--state-dir",
                str(root),
                "--retry-failed",
                "--image",
                "runtime:new",
            ]
        )
        == 2
    )
    assert "Cannot resume batch:" in capsys.readouterr().err
    assert manifest_path.read_bytes() == original_manifest
    assert all(path.read_bytes() == content for path, content in records.items())
    batch_runtime.images.assert_not_called()
    batch_runtime.background.assert_not_called()


@pytest.mark.parametrize("status", ["paused", "running"])
def test_resume_existing_batch_preserves_game_state_and_pinned_images(saved_batch, batch_runtime, status):
    # A supervisor crash can leave the manifest at "running" after games pause.
    root = saved_batch
    manifest_path = root / "batch.json"
    manifest = json.loads(manifest_path.read_text())
    for run in manifest["runs"]:
        run["status"] = status
        run.pop("error", None)
        game_root = root / "games" / run["id"]
        (game_root / "turn-recovery").mkdir()
        (game_root / "turn-recovery/current.json").write_text('{"saved_turn":71}')
        (game_root / "exploration.json").write_bytes(b"Existing native checkpoint")
    manifest_path.write_text(json.dumps(manifest))
    original = {path: path.read_bytes() for path in (root / "games").rglob("*") if path.is_file()}
    batch_runtime.validation.reset_mock()

    assert cli.main(["batch", "resume", "--state-dir", str(root)]) == 0

    resumed = json.loads(manifest_path.read_text())
    assert resumed["format"] == 2 and resumed["parallel"] == manifest["parallel"]
    assert [run["id"] for run in resumed["runs"]] == [run["id"] for run in manifest["runs"]]
    assert all(run["status"] == "pending" for run in resumed["runs"])
    for path, content in original.items():
        if path.name == "container.json":
            assert json.loads(path.read_bytes()) == {**json.loads(content), "launchResumeRequested": True}
        else:
            assert path.read_bytes() == content
    batch_runtime.images.assert_not_called()
    batch_runtime.validation.assert_not_called()
    batch_runtime.background.assert_called_once_with(root, "batch")


@pytest.mark.parametrize("flag", [None, "--fast", "--no-fast"])
@pytest.mark.parametrize("second_status", ["failed", "completed", "budget_exhausted"])
@pytest.mark.parametrize("retry", [False, True])
def test_batch_resume_priority_only_changes_selected_games(saved_batch, batch_runtime, flag, second_status, retry):
    root = saved_batch
    manifest_path = root / "batch.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["runs"][1]["status"] = second_status
    manifest_path.write_text(json.dumps(manifest))
    paths = [root / "games" / run["id"] / "resolved.json" for run in manifest["runs"]]
    first = json.loads(paths[0].read_text())
    first["agents"]["fast"] = True
    paths[0].write_text(json.dumps(first))
    checkpoints = {}
    for path in paths:
        checkpoint = path.parent / "exploration.json"
        checkpoint.write_bytes(b"Preserved native checkpoint")
        checkpoints[checkpoint] = checkpoint.read_bytes()
    originals = {path: path.read_bytes() for path in root.glob("games/*/container.json")}
    arguments = ["batch", "resume", "--state-dir", str(root)]
    if flag:
        arguments.append(flag)
    if retry:
        arguments.append("--retry-failed")
    assert cli.main(arguments) == 0
    expected = [True, False]
    if flag:
        expected[0] = flag == "--fast"
        if retry and second_status == "failed":
            expected[1] = flag == "--fast"
    assert [json.loads(path.read_text())["agents"]["fast"] for path in paths] == expected
    selected = {"g0001", *(["g0002"] if retry and second_status == "failed" else [])}
    for path, content in originals.items():
        if path.parent.name in selected:
            assert json.loads(path.read_bytes()) == {**json.loads(content), "launchResumeRequested": True}
        else:
            assert path.read_bytes() == content
    assert all(path.read_bytes() == content for path, content in checkpoints.items())
    batch_runtime.images.assert_not_called()
    batch_runtime.background.assert_called_once_with(root, "batch")
