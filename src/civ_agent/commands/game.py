"""Launch and control a durable, isolated game using rootless Podman."""

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

from civ_agent.agent_settings import add_settings_arguments, setting_argument
from civ_agent.container_supervisor import validate_routing_credentials, validate_setup
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
from civ_agent.game_config import ResolvedGameConfig, load_config
from civ_agent.monitoring import path_for, read_record
from civ_agent.providers import PROVIDERS
from civ_agent.tls import private_directory, write_private, write_private_json

DEFAULT_STATE = Path.home() / ".local/state/unciv-host/vanilla-full"


class PlayerSetting(argparse.Action):
    def __call__(self, parser, namespace, values, option_string=None):
        player, separator, value = values.partition("=")
        if not separator or not player.strip():
            raise argparse.ArgumentError(self, "expected PLAYER=VALUE")
        value = setting_argument(value)
        current = dict(getattr(namespace, self.dest) or {})
        if player in current:
            raise argparse.ArgumentError(self, "player specified more than once")
        current[player] = value
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
        result.add_argument("--master-seed", type=int, help="Derive every unspecified seed from this value")
        result.add_argument("--map-seed", type=int, help="Override the derived terrain seed")
        result.add_argument("--starting-position-seed", type=int, help="Override the derived native placement seed")
        result.add_argument("--assignment-seed", type=int, help="Player assignment seed, independent of the map seed")
        result.add_argument("--randomize", action=argparse.BooleanOptionalAction, default=None)
        result.add_argument("--civilizations", nargs="+", metavar="CIV")
        result.add_argument("--city-states", type=int)
        for field in ("map-type", "difficulty", "speed", "starting-era"):
            result.add_argument("--" + field)
        sizes = result.add_mutually_exclusive_group()
        sizes.add_argument("--map-size")
        sizes.add_argument("--map-radius", type=int)
        result.add_argument("--victories", nargs="+")
        add_settings_arguments(
            result, model_help="Default agent model", reasoning_help="Default agent reasoning effort"
        )
        result.add_argument("--provider", choices=PROVIDERS, help="Default agent provider")
        result.add_argument(
            "--player-provider", dest="player_providers", action=PlayerSetting, metavar="PLAYER=PROVIDER"
        )
        result.add_argument("--player-model", dest="player_models", action=PlayerSetting, metavar="PLAYER=MODEL")
        result.add_argument(
            "--player-reasoning-effort", dest="player_reasoning_efforts", action=PlayerSetting, metavar="PLAYER=LEVEL"
        )
        result.add_argument(
            "--communication-reasoning-effort",
            type=setting_argument,
            help="Default reasoning effort for background diplomacy (default: cap high/max gameplay at medium)",
        )
        result.add_argument(
            "--player-communication-reasoning-effort",
            dest="player_communication_reasoning_efforts",
            action=PlayerSetting,
            metavar="PLAYER=LEVEL",
        )
        result.add_argument("--fast", action=argparse.BooleanOptionalAction, default=None)
        result.add_argument("--strategy-guide", action=argparse.BooleanOptionalAction, default=None)
        result.add_argument("--civilization-guide", action=argparse.BooleanOptionalAction, default=None)
        result.add_argument("--native-advice-enabled", action=argparse.BooleanOptionalAction, default=None)
        from civ_agent.decisions_policy import add_decisions_arguments

        add_decisions_arguments(result, defaults=False)
        result.add_argument("--review-interval", type=int)
        result.add_argument("--review-attempts", type=int)
    return result


def resolve_options(args):
    overrides = {}
    from civ_agent.decisions_policy import DECISIONS_FIELDS

    fields = {
        "game": (
            "master_seed",
            "map_seed",
            "starting_position_seed",
            "assignment_seed",
            "randomize",
            "civilizations",
            "city_states",
            "map_type",
            "map_size",
            "map_radius",
            "difficulty",
            "speed",
            "starting_era",
            "victories",
        ),
        "agents": (
            "provider",
            "model",
            "reasoning_effort",
            "communication_reasoning_effort",
            "fast",
            "strategy_guide",
            "civilization_guide",
            "native_advice_enabled",
            *DECISIONS_FIELDS,
            "review_interval",
            "review_attempts",
        ),
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
    player_overrides = {}
    for option, setting in (
        ("player_providers", "provider"),
        ("player_models", "model"),
        ("player_reasoning_efforts", "reasoning_effort"),
        ("player_communication_reasoning_efforts", "communication_reasoning_effort"),
    ):
        for name, value in (getattr(args, option, None) or {}).items():
            player_overrides.setdefault(name, {})[setting] = value
    return load_config(getattr(args, "config", None), overrides, player_overrides=player_overrides).resolved()


def initialize(root, config, auth_root, *, original=None, images=None, validation=None):
    if auth_root.resolve().is_relative_to(root.resolve()):
        raise ContainerError("The provider credential directory must be outside the match directory")
    if (root / "resolved.json").exists() or (root / "exploration.json").exists():
        raise ContainerError("A saved game exists; use game resume or a new --state-dir")
    validate_routing_credentials(config, auth_root)
    images = images or {
        "runtime": resolve_image(config.runtime.image),
        **({"desktop": resolve_image(config.runtime.desktop_image)} if config.human else {}),
    }
    validation = validation or validate_setup(config, images["runtime"])
    if config.limits.max_turns is None:
        config.limits.max_turns = validation["maxTurns"] + 1
    private_directory(root)
    record = identity(root)
    if config.agents.decisions_enabled:
        from civ_agent.decisions_ledger import DecisionsLedger

        record["decisionsAuthority"] = DecisionsLedger(auth_root).authority
    from civ_agent.decisions_tasks import TASK_VERSION
    from civ_agent.diplomacy_decisions import MODEL, POLICY_VERSION

    record.update(
        images=images,
        auth_root=str(auth_root),
        native_setup=validation,
        experiments={
            "native_advice_enabled": config.agents.native_advice_enabled,
            "decisions_enabled": config.agents.decisions_enabled,
            "nativeGuideVersion": "native-guidance-v1",
            "classifierModel": MODEL,
            "routingPolicyVersion": POLICY_VERSION,
            "advisoryPolicyVersion": TASK_VERSION,
        },
    )
    write_private_json(root / "container.json", record)
    write_private_json(root / "resolved.json", config.model_dump())
    if original is not None:
        write_private(root / "input.toml", original.read_bytes())
    return record


def resume_configuration(root, args):
    config = ResolvedGameConfig.model_validate(read_json(root / "resolved.json"))
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
        selected_auth = args.auth_dir.expanduser().resolve()
        if config.agents.decisions_enabled and selected_auth != Path(record["auth_root"]).resolve():
            raise ValueError("Cannot change auth directory: this game's Decisions accounting authority is pinned")
        record["auth_root"] = str(selected_auth)
    for key, option in (("runtime", "image"), ("desktop", "desktop_image")):
        if getattr(args, option) is not None:
            record["images"][key] = resolve_image(getattr(args, option))
    config = ResolvedGameConfig.model_validate(config.model_dump())
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
            from civ_agent.experiment_status import experiment_status

            status = read_json(root / "container-status.json", {})
            result = read_record(path_for(root), "state", "result") if path_for(root).exists() else None
            saved = read_json(root / "resolved.json")
            roster = ResolvedGameConfig.model_validate(saved) if saved else None
            print(
                json.dumps(
                    {
                        "containers": status,
                        "browser": read_json(root / "browser.json"),
                        "result": result,
                        "players": roster.player_records() if roster else [],
                        "experiments": experiment_status(root, roster, read_json(root / "container.json", {})),
                    },
                    indent=2,
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
                print(
                    json.dumps(
                        {
                            "config": config.model_dump(),
                            "turn_order": [p.civilization for p in config.turn_order],
                            "native_setup": native,
                        },
                        indent=2,
                    )
                )
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
