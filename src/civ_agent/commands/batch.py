"""Generate one seeded TOML per game and run the saved queue with bounded concurrency."""

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

from civ_agent.benchmark import generate_runs, seeded_input
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
from civ_agent.game_config import SEED_FIELDS, ResolvedGameConfig, load_config
from civ_agent.tls import write_private, write_private_json


def parser(*, add_help=True, action="start"):
    result = argparse.ArgumentParser(description=__doc__, add_help=add_help)
    result.set_defaults(action=action)
    result.add_argument("--state-dir", type=Path, required=True)
    if action in {"generate", "start"}:
        result.add_argument("--config", type=Path, help="Ordinary single-game TOML template")
        result.add_argument("--games", type=int, help="Number of independent games to generate")
        result.add_argument("--master-seed", type=int, help="First game's master seed; incremented once per game")
        result.add_argument(
            "--dry-run", action="store_true", help="Show generated settings without writing or launching"
        )
    if action == "start":
        result.add_argument("--auth-dir", type=Path, default=DEFAULT_AUTH)
    if action in {"start", "resume"}:
        result.add_argument("--parallel", type=int)
    if action == "resume":
        result.add_argument("--retry-failed", action="store_true")
        result.add_argument("--image", help="Explicit runtime upgrade for unfinished runs")
        result.add_argument("--desktop-image", help="Explicit desktop upgrade for unfinished runs")
    return result


def materialize_inputs(root, manifest):
    """Complete interrupted generation from the durable queue, without reseeding."""
    template = (root / "input.toml").read_text()
    for row in manifest["runs"]:
        path = root / "games" / row["id"] / "input.toml"
        if not path.exists():
            config = ResolvedGameConfig.model_validate(row["config"])
            write_private(path, seeded_input(template, config).encode())


def run(args):
    try:
        os.umask(0o077)
        root = args.state_dir.expanduser().resolve()
        if args.action == "stop":
            stop_game(root, role="batch")
            print(f"Batch stopped; all saved games retained at {root}")
            return 0
        if args.action == "status":
            manifest = read_json(root / "batch.json")
            if not manifest:
                raise ContainerError("No saved batch at this path")
            print(
                json.dumps(
                    {
                        "parallel": manifest["parallel"],
                        "runs": [
                            {
                                "id": r["id"],
                                "status": r["status"],
                                **{name: r["config"]["game"][name] for name in ("master_seed", *SEED_FIELDS)},
                                "assignments": r["config"]["assignments"],
                                "input": str(root / "games" / r["id"] / "input.toml"),
                                "state_dir": str(root / "games" / r["id"]),
                                "game": read_json(root / "games" / r["id"] / "container-status.json", {}),
                                "browser": read_json(root / "games" / r["id"] / "browser.json"),
                            }
                            for r in manifest["runs"]
                        ],
                    },
                    indent=2,
                )
            )
            return 0

        parallel = getattr(args, "parallel", None)
        if parallel is not None and parallel < 1:
            raise ValueError("parallel must be positive")
        source = None
        generated = None
        if args.action in {"generate", "start"}:
            if args.config is not None:
                if args.games is None:
                    raise ValueError("Specify --games with a single-game --config template")
                source = args.config.read_text()
                generated = {
                    "parallel": parallel or 1,
                    "runs": generate_runs(load_config(args.config), args.games, args.master_seed),
                }
            elif args.games is not None or args.master_seed is not None or args.action == "generate":
                raise ValueError("Generating games requires --config and --games")
            if args.dry_run:
                manifest = generated or read_json(root / "batch.json")
                if not manifest:
                    raise ContainerError("Specify --config and --games, or an existing generated batch")
                print(json.dumps(manifest, indent=2))
                return 0

        with locked(root / ".launch.lock"), locked(root / ".supervisor.lock"):
            manifest = read_json(root / "batch.json")
            if generated is not None:
                if manifest is not None:
                    raise ContainerError("A batch exists; start the generated batch or resume it without --config")
                if (root / "games").exists() and any((root / "games").iterdir()):
                    raise ContainerError("Game folders already exist; choose a new --state-dir")
                assert source is not None
                write_private(root / "input.toml", source.encode())
                manifest = generated
                # Persist the complete schedule first: an interrupted write must
                # never turn a request for N games into a smaller batch.
                write_private_json(root / "batch.json", manifest)
            if not manifest:
                raise ContainerError("No saved batch; generate one with --config and --games")
            if "images" not in manifest:
                materialize_inputs(root, manifest)
            if args.action == "generate":
                print(
                    f"Generated {len(manifest['runs'])} game inputs: {root / 'games'}\nLaunch: civ-agent batch start --state-dir {root}"
                )
                return 0
            if args.action == "start" and "images" in manifest:
                raise ContainerError("This batch has started; use batch resume")
            if args.action == "resume" and "images" not in manifest:
                raise ContainerError("This batch has only been generated; use batch start")
            if parallel is not None:
                manifest["parallel"] = parallel

            preflight()
            identity(root)
            if args.action == "start":
                # Generated files are ordinary editable game inputs until start.
                # From here on, resume uses only frozen configuration and saves.
                configs = [
                    load_config(root / "games" / row["id"] / "input.toml").resolved() for row in manifest["runs"]
                ]
                first = configs[0]
                if any(
                    (c.runtime.image, c.runtime.desktop_image) != (first.runtime.image, first.runtime.desktop_image)
                    for c in configs
                ):
                    raise ValueError("All games in a batch must select the same runtime and desktop images")
                manifest["images"] = {
                    "runtime": resolve_image(first.runtime.image),
                    **(
                        {"desktop": resolve_image(first.runtime.desktop_image)} if any(c.human for c in configs) else {}
                    ),
                }
                manifest["auth_root"] = str(args.auth_dir.expanduser().resolve())
                for row, config in zip(manifest["runs"], configs, strict=True):
                    row["config"] = config.model_dump()
            else:
                for key, option in (("runtime", "image"), ("desktop", "desktop_image")):
                    if getattr(args, option, None):
                        manifest["images"][key] = resolve_image(getattr(args, option))
                for row in manifest["runs"]:
                    if row["status"] in {"running", "paused"} or (args.retry_failed and row["status"] == "failed"):
                        row["status"] = "pending"
            for row in manifest["runs"]:
                config = ResolvedGameConfig.model_validate(row["config"])
                if config.human and manifest["parallel"] > 1 and config.runtime.browser_port:
                    raise ValueError("Concurrent human games need runtime.browser_port = 0")
            write_private_json(root / "batch.json", manifest)
            for row in manifest["runs"]:
                match = root / "games" / row["id"]
                if not (match / "resolved.json").exists():
                    initialize(
                        match,
                        ResolvedGameConfig.model_validate(row["config"]),
                        Path(manifest["auth_root"]),
                        images=manifest["images"],
                    )
                elif row["status"] == "pending" and args.action == "resume":
                    with locked(match / ".supervisor.lock"):
                        record = identity(match)
                        for key, option in (("runtime", "image"), ("desktop", "desktop_image")):
                            if getattr(args, option, None):
                                record["images"][key] = manifest["images"][key]
                        write_private_json(match / "container.json", record)
                row["config"] = read_json(match / "resolved.json")
                write_private_json(root / "batch.json", manifest)
        unit = launch_background(root, "batch")
        print(
            f"Started: {unit}\nGames: {len(manifest['runs'])}\nConcurrent games: {manifest['parallel']}\nState: {root}"
        )
        return 0
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
        print(f"Cannot {args.action} batch: {error}", file=sys.stderr)
        return 2
