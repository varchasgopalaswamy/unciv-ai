"""Run a persisted TOML benchmark matrix with bounded game concurrency."""

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

from civ_agent.commands.game import initialize, stop_game
from civ_agent.container_supervisor import validate_setup
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
from civ_agent.game_config import GameConfig, expand_batch, load_config
from civ_agent.tls import private_directory, write_private, write_private_json


def parser(*, add_help=True, action="start"):
    result = argparse.ArgumentParser(description=__doc__, add_help=add_help)
    result.set_defaults(action=action)
    result.add_argument("--state-dir", type=Path, required=True)
    if action == "start":
        result.add_argument("--config", type=Path, required=True)
        result.add_argument("--auth-dir", type=Path, default=DEFAULT_AUTH)
        result.add_argument("--dry-run", action="store_true")
    if action in {"start", "resume"}:
        result.add_argument("--parallel", type=int)
    if action == "resume":
        result.add_argument("--retry-failed", action="store_true")
        result.add_argument("--image", help="Explicit runtime upgrade for unfinished runs")
        result.add_argument("--desktop-image", help="Explicit desktop upgrade for unfinished runs")
    return result


def run(args):
    config = None
    runs = []
    manifest: dict[str, Any]
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
        if args.action == "start":
            config = load_config(
                args.config, {"batch": {"parallel": args.parallel}} if args.parallel is not None else None
            ).resolved()
            if config.human and config.batch.parallel > 1 and config.runtime.browser_port:
                raise ValueError("Concurrent human games need runtime.browser_port = 0")
            runs = expand_batch(config)
            if args.dry_run:
                print(json.dumps({"parallel": config.batch.parallel, "runs": runs}, indent=2))
                return 0
        preflight()
        private_directory(root)
        with locked(root / ".launch.lock"), locked(root / ".supervisor.lock"):
            if args.action == "start":
                assert config is not None
                if (root / "batch.json").exists():
                    raise ContainerError("A batch exists; use batch resume")
                identity(root)
                images = {
                    "runtime": resolve_image(config.runtime.image),
                    **({"desktop": resolve_image(config.runtime.desktop_image)} if config.human else {}),
                }
                validation = validate_setup(config, images["runtime"])
                for row in runs:
                    game_config = GameConfig.model_validate(row["config"])
                    if game_config.limits.max_turns is None:
                        game_config.limits.max_turns = validation["maxTurns"] + 1
                    row["config"] = game_config.model_dump()
                manifest = {
                    "parallel": config.batch.parallel,
                    "runs": runs,
                    "images": images,
                    "native_setup": validation,
                    "auth_root": str(args.auth_dir.expanduser().resolve()),
                }
                write_private(root / "input.toml", args.config.read_bytes())
            else:
                manifest = read_json(root / "batch.json")
                if not manifest:
                    raise ContainerError("No saved batch at this path")
                for key, option in (("runtime", "image"), ("desktop", "desktop_image")):
                    if getattr(args, option, None):
                        manifest["images"][key] = resolve_image(getattr(args, option))
                if args.parallel is not None:
                    if args.parallel < 1:
                        raise ValueError("parallel must be positive")
                    manifest["parallel"] = args.parallel
                for row in manifest["runs"]:
                    if row["status"] in {"running", "paused"} or (args.retry_failed and row["status"] == "failed"):
                        row["status"] = "pending"
            write_private_json(root / "batch.json", manifest)
            # The queue is durable before preparing any run. Resume can finish
            # initialization after an interruption without re-expanding the matrix.
            for row in manifest["runs"]:
                match = root / "games" / row["id"]
                if not (match / "resolved.json").exists():
                    initialize(
                        match,
                        GameConfig.model_validate(row["config"]),
                        Path(manifest["auth_root"]),
                        images=manifest["images"],
                        validation=manifest["native_setup"],
                    )
                elif row["status"] == "pending" and args.action == "resume":
                    with locked(match / ".supervisor.lock"):
                        record = identity(match)
                        for key, option in (("runtime", "image"), ("desktop", "desktop_image")):
                            if getattr(args, option, None):
                                record["images"][key] = manifest["images"][key]
                        write_private_json(match / "container.json", record)
        unit = launch_background(root, "batch")
        print(
            f"Started: {unit}\nGames: {len(manifest['runs'])}\nConcurrent games: {manifest['parallel']}\nState: {root}"
        )
        return 0
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
        print(f"Cannot {args.action} batch: {error}", file=sys.stderr)
        return 2
