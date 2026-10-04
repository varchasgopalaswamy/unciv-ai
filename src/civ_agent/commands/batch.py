"""Generate reusable TOML inputs and run them with bounded concurrency."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import tomllib
from dataclasses import dataclass
from pathlib import Path

from civ_agent.benchmark import input_plans
from civ_agent.commands.game import initialize, stop_game
from civ_agent.container_support import (
    DEFAULT_AUTH,
    ContainerError,
    identity,
    launch_background,
    locked,
    preflight,
    read_json,
    resolve_image,
)
from civ_agent.game_config import GameConfig, ResolvedGameConfig, load_config
from civ_agent.tls import write_private, write_private_json

BATCH_FORMAT = 2


@dataclass(frozen=True)
class InputFile:
    name: str
    path: Path
    content: bytes


@dataclass(frozen=True)
class ResolvedInput:
    input: InputFile
    config: ResolvedGameConfig


def parser(*, add_help=True, action="start"):
    result = argparse.ArgumentParser(description=__doc__, add_help=add_help)
    result.set_defaults(action=action)
    if action == "generate":
        result.add_argument("--config", type=Path, required=True, help="Ordinary single-game TOML template")
        result.add_argument("--games", type=int, required=True, help="Number of independent inputs to generate")
        result.add_argument("--output", type=Path, required=True, help="New directory for generated TOML inputs")
        result.add_argument("--batch-seed", type=int, help="Reproduce this generated input set")
        result.add_argument("--dry-run", action="store_true", help="Preview inputs without publishing them")
    elif action == "start":
        result.add_argument("--inputs", type=Path, required=True, help="Directory of single-game TOML inputs")
        result.add_argument("--state-dir", type=Path, required=True)
        result.add_argument("--auth-dir", type=Path, default=DEFAULT_AUTH)
        result.add_argument("--parallel", type=int)
        result.add_argument("--dry-run", action="store_true", help="Preview resolved games without starting them")
    else:
        result.add_argument("--state-dir", type=Path, required=True)
        if action in {"start", "resume"}:
            result.add_argument("--parallel", type=int)
        if action == "resume":
            result.add_argument("--retry-failed", action="store_true")
            result.add_argument("--image", help="Explicit runtime image upgrade for unfinished runs")
            result.add_argument("--desktop-image", help="Explicit desktop image upgrade for unfinished runs")
    return result


def _path_exists(path: Path) -> bool:
    return path.exists() or path.is_symlink()


def _input_files(directory: Path) -> list[InputFile]:
    directory = directory.expanduser().resolve()
    if not directory.is_dir():
        raise ContainerError(f"Input directory does not exist: {directory}")
    files = sorted(
        (path for path in directory.iterdir() if path.is_file() and path.suffix == ".toml"), key=lambda p: p.name
    )
    if not files:
        raise ContainerError(f"Input directory contains no TOML files: {directory}")
    names = [path.stem for path in files]
    if any(not name or name in {".", ".."} or Path(name).name != name for name in names):
        raise ContainerError("Input TOML filenames must have safe, nonempty stems")
    if len(names) != len(set(names)):
        raise ContainerError("Input TOML stems must be unique")
    return [InputFile(name=path.stem, path=path, content=path.read_bytes()) for path in files]


def _resolved_inputs(directory: Path) -> list[ResolvedInput]:
    files = _input_files(directory)
    return [
        ResolvedInput(
            input=input_file,
            config=GameConfig.model_validate(tomllib.loads(input_file.content.decode("utf-8"))).resolved(),
        )
        for input_file in files
    ]


def _validate_parallel(parallel: int) -> int:
    if parallel < 1:
        raise ValueError("parallel must be positive")
    return parallel


def _validate_batch_configs(configs: list[ResolvedGameConfig], parallel: int) -> None:
    if not configs:
        raise ValueError("A batch needs at least one input")
    _validate_parallel(parallel)
    first_images = (configs[0].runtime.image, configs[0].runtime.desktop_image)
    if any((config.runtime.image, config.runtime.desktop_image) != first_images for config in configs[1:]):
        raise ValueError("All games in a batch must select the same runtime and desktop images")
    if any(config.human and parallel > 1 and config.runtime.browser_port for config in configs):
        raise ValueError("Concurrent human games need runtime.browser_port = 0")


def _preview(resolved: list[ResolvedInput], parallel: int) -> dict:
    return {
        "parallel": parallel,
        "inputs": [
            {
                "id": item.input.name,
                "filename": item.input.path.name,
                "config": item.config.model_dump(),
            }
            for item in resolved
        ],
    }


def _publish_inputs(output: Path, plans: list[dict]) -> None:
    output = output.expanduser()
    if _path_exists(output):
        raise ContainerError(f"Output directory already exists: {output.resolve()}")
    output = output.resolve()
    if _path_exists(output):
        raise ContainerError(f"Output directory already exists: {output}")
    output.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=f".{output.name}.", dir=output.parent))
    try:
        for plan in plans:
            write_private(staging / plan["name"], plan["content"].encode())
        if _path_exists(output):
            raise ContainerError(f"Output directory already exists: {output}")
        # The destination is checked before the rename and is never replaced.
        os.rename(staging, output)
        staging = None
    except FileExistsError as error:
        raise ContainerError(f"Output directory already exists: {output}") from error
    finally:
        if staging is not None and staging.exists():
            shutil.rmtree(staging)


def _load_manifest(root: Path) -> dict:
    manifest = read_json(root / "batch.json")
    if not isinstance(manifest, dict) or manifest.get("format") != BATCH_FORMAT:
        raise ContainerError("Unsupported batch manifest; start a new batch")
    runs = manifest.get("runs")
    if not isinstance(runs, list) or not runs:
        raise ContainerError("Invalid batch manifest")
    for row in runs:
        if (
            not isinstance(row, dict)
            or set(row) - {"id", "status", "error"}
            or not isinstance(row.get("id"), str)
            or not row["id"]
            or Path(row["id"]).name != row["id"]
            or row["id"] in {".", ".."}
            or not isinstance(row.get("status"), str)
        ):
            raise ContainerError("Invalid batch manifest")
    if not isinstance(manifest.get("parallel"), int) or manifest["parallel"] < 1:
        raise ContainerError("Invalid batch concurrency")
    return manifest


def _saved_config(root: Path, game_id: str) -> ResolvedGameConfig:
    path = root / "games" / game_id / "resolved.json"
    saved = read_json(path)
    if saved is None:
        raise ContainerError(f"Saved configuration is missing for game {game_id}")
    try:
        return ResolvedGameConfig.model_validate(saved)
    except ValueError as error:
        raise ContainerError(f"Saved configuration is invalid for game {game_id}") from error


def _status(root: Path, manifest: dict) -> dict:
    rows = []
    for run in manifest["runs"]:
        game_id = run["id"]
        config = _saved_config(root, game_id)
        game_root = root / "games" / game_id
        rows.append(
            {
                "id": game_id,
                "status": run["status"],
                "master_seed": config.game.master_seed,
                "map_seed": config.game.map_seed,
                "starting_position_seed": config.game.starting_position_seed,
                "assignment_seed": config.game.assignment_seed,
                "assignments": [assignment.model_dump() for assignment in config.assignments],
                "input": str(game_root / "input.toml"),
                "state_dir": str(game_root),
                "game": read_json(game_root / "container-status.json", {}),
                "browser": read_json(game_root / "browser.json"),
            }
        )
    return {"parallel": manifest["parallel"], "runs": rows}


def _prepare_batch(
    root: Path,
    resolved: list[ResolvedInput],
    parallel: int,
    auth_root: Path,
    images: dict,
) -> dict:
    root = root.expanduser()
    auth_root = auth_root.expanduser().resolve()
    if _path_exists(root):
        raise ContainerError(f"State directory already exists: {root.resolve()}")
    root = root.resolve()
    if auth_root.is_relative_to(root):
        raise ContainerError("The provider credential directory must be outside the batch state directory")
    if _path_exists(root):
        raise ContainerError(f"State directory already exists: {root}")
    root.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=f".{root.name}.", dir=root.parent))
    try:
        identity(staging)
        for item in resolved:
            game_root = staging / "games" / item.input.name
            initialize(game_root, item.config, auth_root, images=images)
            write_private(game_root / "input.toml", item.input.content)
        manifest = {
            "format": BATCH_FORMAT,
            "parallel": parallel,
            "runs": [{"id": item.input.name, "status": "pending"} for item in resolved],
        }
        write_private_json(staging / "batch.json", manifest)
        if _path_exists(root):
            raise ContainerError(f"State directory already exists: {root}")
        os.rename(staging, root)
        staging = None
        return manifest
    except FileExistsError as error:
        raise ContainerError(f"State directory already exists: {root}") from error
    finally:
        if staging is not None and staging.exists():
            shutil.rmtree(staging)


def _start(args) -> int:
    parallel = _validate_parallel(args.parallel if args.parallel is not None else 1)
    resolved = _resolved_inputs(args.inputs)
    _validate_batch_configs([item.config for item in resolved], parallel)
    if args.dry_run:
        print(json.dumps(_preview(resolved, parallel), indent=2))
        return 0
    root = args.state_dir.expanduser()
    if _path_exists(root):
        raise ContainerError(f"State directory already exists: {root.resolve()}")
    root = root.resolve()
    preflight()
    first = resolved[0].config
    images = {
        "runtime": resolve_image(first.runtime.image),
        **(
            {"desktop": resolve_image(first.runtime.desktop_image)}
            if any(item.config.human for item in resolved)
            else {}
        ),
    }
    manifest = _prepare_batch(root, resolved, parallel, args.auth_dir, images)
    unit = launch_background(root, "batch")
    print(f"Started: {unit}\nGames: {len(manifest['runs'])}\nConcurrent games: {manifest['parallel']}\nState: {root}")
    return 0


def _resume(args) -> int:
    root = args.state_dir.expanduser().resolve()
    manifest = _load_manifest(root)
    parallel = _validate_parallel(args.parallel if args.parallel is not None else manifest["parallel"])
    preflight()
    with locked(root / ".launch.lock"), locked(root / ".supervisor.lock"):
        manifest = _load_manifest(root)
        _validate_batch_configs([_saved_config(root, run["id"]) for run in manifest["runs"]], parallel)
        manifest["parallel"] = parallel
        image = resolve_image(args.image) if args.image else None
        desktop_image = resolve_image(args.desktop_image) if args.desktop_image else None
        for run in manifest["runs"]:
            retry = args.retry_failed and run["status"] == "failed"
            resumable = run["status"] in {"pending", "starting", "running", "paused"} or retry
            if not resumable:
                continue
            run["status"] = "pending"
            run.pop("error", None)
            game_root = root / "games" / run["id"]
            if image is not None or desktop_image is not None:
                record = identity(game_root)
                if image is not None:
                    record["images"]["runtime"] = image
                if desktop_image is not None:
                    record["images"]["desktop"] = desktop_image
                write_private_json(game_root / "container.json", record)
        write_private_json(root / "batch.json", manifest)
    unit = launch_background(root, "batch")
    print(f"Started: {unit}\nGames: {len(manifest['runs'])}\nConcurrent games: {manifest['parallel']}\nState: {root}")
    return 0


def _generate(args) -> int:
    template = args.config.read_text()
    config = load_config(args.config)
    batch_seed, plans = input_plans(template, config, args.games, args.batch_seed)
    output = args.output.expanduser()
    if _path_exists(output):
        raise ContainerError(f"Output directory already exists: {output.resolve()}")
    if args.dry_run:
        print(json.dumps({"batch_seed": batch_seed, "inputs": plans}, indent=2))
        return 0
    _publish_inputs(output, plans)
    print(f"Batch seed: {batch_seed}\nGenerated {len(plans)} inputs: {output}")
    return 0


def run(args):
    try:
        os.umask(0o077)
        if args.action == "generate":
            return _generate(args)
        root = args.state_dir.expanduser().resolve()
        if args.action == "stop":
            _load_manifest(root)
            stop_game(root, role="batch")
            print(f"Batch stopped; all saved games retained at {root}")
            return 0
        if args.action == "status":
            print(json.dumps(_status(root, _load_manifest(root)), indent=2))
            return 0
        if args.action == "start":
            return _start(args)
        if args.action == "resume":
            return _resume(args)
        raise ValueError(f"Unknown batch action: {args.action}")
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
        print(f"Cannot {args.action} batch: {error}", file=sys.stderr)
        return 2
