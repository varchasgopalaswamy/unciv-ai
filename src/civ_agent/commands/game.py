"""Launch and control a durable, isolated game using rootless Podman."""

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

from civ_agent.agent_settings import add_settings_arguments, setting_argument
from civ_agent.container_supervisor import validate_setup
from civ_agent.container_support import (
    DEFAULT_AUTH,
    ContainerError,
    LockBusy,
    cleanup,
    command,
    identity,
    launch_background,
    locked,
    preflight,
    read_json,
    resolve_image,
)
from civ_agent.game_config import GameConfig, load_config
from civ_agent.monitoring import path_for, read_record
from civ_agent.tls import private_directory, write_private, write_private_json

DEFAULT_STATE = Path.home() / ".local/state/unciv-host/vanilla-full"


class AgentSetting(argparse.Action):
    def __call__(self, parser, namespace, values, option_string=None):
        civilization, separator, value = values.partition("=")
        if not separator or not civilization.strip():
            raise argparse.ArgumentError(self, "expected CIV=VALUE")
        value = setting_argument(value)
        current = dict(getattr(namespace, self.dest) or {})
        if civilization in current:
            raise argparse.ArgumentError(self, "civilization specified more than once")
        current[civilization] = value
        setattr(namespace, self.dest, current)


def parser(*, add_help=True, action="start"):
    result = argparse.ArgumentParser(description=__doc__, add_help=add_help)
    result.set_defaults(action=action)
    result.add_argument("--state-dir", type=Path, default=DEFAULT_STATE)
    if action in {"stop", "status", "remove"}:
        return result
    result.add_argument("--auth-dir", type=Path, help="Durable benchmark login directory")
    result.add_argument("--image", help="Explicit runtime image; otherwise resume retains the saved image")
    result.add_argument("--desktop-image", help="Explicit native desktop image")
    result.add_argument("--port", type=int, help="Browser port; 0 chooses an available localhost port")
    for field in ("max-turns", "max-invocations", "max-seconds", "turn-timeout", "startup-timeout"):
        result.add_argument("--" + field, type=int)
    if action == "start":
        result.add_argument("--config", type=Path, help="TOML game configuration")
        result.add_argument(
            "--dry-run", action="store_true", help="Validate setup and show settings without starting models"
        )
        result.add_argument("--seed", type=int)
        result.add_argument("--city-states", type=int)
        for field in ("map-type", "difficulty", "speed", "starting-era"):
            result.add_argument("--" + field)
        sizes = result.add_mutually_exclusive_group()
        sizes.add_argument("--map-size")
        sizes.add_argument("--map-radius", type=int)
        result.add_argument("--victories", nargs="+")
        result.add_argument("--agents", nargs="+", metavar="CIV")
        result.add_argument("--human", metavar="CIV")
        result.add_argument("--built-in-ai", nargs="*", metavar="CIV")
        add_settings_arguments(
            result, model_help="Default agent model", reasoning_help="Default agent reasoning effort"
        )
        result.add_argument("--agent-model", dest="agent_models", action=AgentSetting, metavar="CIV=MODEL")
        result.add_argument(
            "--agent-reasoning-effort", dest="agent_reasoning_efforts", action=AgentSetting, metavar="CIV=LEVEL"
        )
        result.add_argument("--fast", action=argparse.BooleanOptionalAction, default=None)
        result.add_argument("--strategy-guide", action=argparse.BooleanOptionalAction, default=None)
        result.add_argument("--review-interval", type=int)
        result.add_argument("--review-attempts", type=int)
    return result


def resolve_options(args):
    overrides = {}
    fields = {
        "game": (
            "seed",
            "city_states",
            "map_type",
            "map_size",
            "map_radius",
            "difficulty",
            "speed",
            "starting_era",
            "victories",
        ),
        "agents": ("model", "reasoning_effort", "fast", "strategy_guide", "review_interval", "review_attempts"),
        "limits": ("max_turns", "max_invocations", "max_seconds", "turn_timeout", "startup_timeout"),
    }
    for section, names in fields.items():
        overrides[section] = {name: getattr(args, name) for name in names if getattr(args, name, None) is not None}
    if getattr(args, "map_radius", None) is not None:
        overrides["game"]["map_size"] = None
    if getattr(args, "map_size", None) is not None:
        overrides["game"]["map_radius"] = None
    overrides["runtime"] = {}
    for source, target in (("image", "image"), ("desktop_image", "desktop_image"), ("port", "browser_port")):
        if getattr(args, source, None) is not None:
            overrides["runtime"][target] = getattr(args, source)
    config = load_config(getattr(args, "config", None), overrides)
    data = config.model_dump()
    for option, controller in (("agents", "agent"), ("built_in_ai", "built_in_ai")):
        names = getattr(args, option, None)
        if names is not None:
            data["players"] = [p for p in data["players"] if p["controller"] != controller]
            data["players"] += [{"civilization": name, "controller": controller} for name in names]
    if getattr(args, "human", None):
        data["players"] = [p for p in data["players"] if p["civilization"] != args.human and p["controller"] != "human"]
        data["players"].insert(0, {"civilization": args.human, "controller": "human"})
    for option, setting in (("agent_models", "model"), ("agent_reasoning_efforts", "reasoning_effort")):
        for civilization, value in (getattr(args, option, None) or {}).items():
            selected = next(
                (p for p in data["players"] if p["civilization"] == civilization and p["controller"] == "agent"), None
            )
            if selected is None:
                raise ValueError(f"{civilization} is not an agent player")
            selected[setting] = value
    return GameConfig.model_validate(data).resolved()


def initialize(root, config, auth_root, *, original=None, images=None, validation=None):
    if (root / "resolved.json").exists() or (root / "exploration.json").exists():
        raise ContainerError("A saved game exists; use game resume or a new --state-dir")
    images = images or {
        "runtime": resolve_image(config.runtime.image),
        **({"desktop": resolve_image(config.runtime.desktop_image)} if config.human else {}),
    }
    validation = validation or validate_setup(config, images["runtime"])
    if config.limits.max_turns is None:
        config.limits.max_turns = validation["maxTurns"] + 1
    private_directory(root)
    record = identity(root)
    record.update(images=images, auth_root=str(auth_root), native_setup=validation)
    write_private_json(root / "container.json", record)
    write_private_json(root / "resolved.json", config.model_dump())
    if original is not None:
        write_private(root / "input.toml", original.read_bytes())
    return record


def resume_configuration(root, args):
    config = GameConfig.model_validate(read_json(root / "resolved.json"))
    record = identity(root)
    for key in ("max_turns", "max_invocations", "max_seconds", "turn_timeout", "startup_timeout"):
        value = getattr(args, key, None)
        if value is not None:
            if value < getattr(config.limits, key):
                raise ValueError("Resume cannot decrease saved run limits")
            setattr(config.limits, key, value)
    if args.port is not None:
        config.runtime.browser_port = args.port
    if args.auth_dir is not None:
        record["auth_root"] = str(args.auth_dir.expanduser().resolve())
    for key, option in (("runtime", "image"), ("desktop", "desktop_image")):
        if getattr(args, option) is not None:
            record["images"][key] = resolve_image(getattr(args, option))
    config = GameConfig.model_validate(config.model_dump())
    write_private_json(root / "container.json", record)
    write_private_json(root / "resolved.json", config.model_dump())
    return config


def stop_game(root, *, role="game"):
    record = read_json(root / "container.json")
    if record is None:
        raise ContainerError("No containerized run at this path")
    write_private_json(root / "control/request.json", {"action": "stop"})
    result = command(["systemctl", "--user", "stop", f"civ-{role}-{record['id']}"], check=False, timeout=250)
    if role == "batch":
        for row in read_json(root / "batch.json")["runs"]:
            match = root / "games" / row["id"]
            if (match / "container.json").exists():
                stop_game(match)
    elif result.returncode:
        # A batch owns its games in the batch supervisor, not separate systemd
        # units. Let their controller consume the stop request before removing
        # orphaned infrastructure. No model work is polled or restarted here.
        deadline = time.monotonic() + 120
        while True:
            try:
                with locked(root / ".supervisor.lock"):
                    cleanup(record["id"])
                break
            except LockBusy:
                if time.monotonic() >= deadline:
                    cleanup(record["id"])
                    break
                time.sleep(0.2)


def run(args):
    config = None
    try:
        os.umask(0o077)
        root = args.state_dir.expanduser().resolve()
        if args.action == "status":
            status = read_json(root / "container-status.json", {})
            result = read_record(path_for(root), "state", "result") if path_for(root).exists() else None
            print(
                json.dumps(
                    {"containers": status, "browser": read_json(root / "browser.json"), "result": result}, indent=2
                )
            )
            return 0
        if args.action in {"stop", "remove"}:
            stop_game(root)
            if args.action == "remove":
                cleanup(identity(root)["id"])
            print(f"Stopped. Durable state retained: {root}")
            return 0
        if args.action == "start":
            config = resolve_options(args)
            if args.dry_run:
                native = validate_setup(config, resolve_image(config.runtime.image))
                print(json.dumps({"config": config.model_dump(), "native_setup": native}, indent=2))
                return 0
        preflight()
        private_directory(root)
        with locked(root / ".launch.lock"):
            with locked(root / ".supervisor.lock"):
                if args.action == "start":
                    assert config is not None
                    initialize(
                        root, config, (args.auth_dir or DEFAULT_AUTH).expanduser().resolve(), original=args.config
                    )
                else:
                    resume_configuration(root, args)
            unit = launch_background(root, "game")
        print(f"Started: {unit}\nState: {root}\nLogs: {root / 'supervisor.log'}")
        print(f"Dashboard: civ-agent match watch --state-dir {root}")
        print(f"Status and browser address: civ-agent game status --state-dir {root}")
        return 0
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
        print(f"Cannot {args.action} game: {error}", file=sys.stderr)
        return 2
