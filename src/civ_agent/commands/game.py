"""Launch and control a durable, isolated game using rootless Podman."""

import argparse
import json
import os
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from civ_agent.container_identity import deployment_identity, require_local_runtime
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
from civ_agent.player_settings import add_settings_arguments, setting_argument
from civ_agent.provider_credentials import PROVIDERS
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
    result.add_argument("--record-replay", action="store_true", help="Record game events for private replay")
    result.add_argument("--port", type=int, help="Browser port; 0 chooses an available localhost port")
    for field in ("max-turns", "max-invocations", "max-seconds", "turn-timeout", "startup-timeout"):
        result.add_argument("--" + field, type=int)
    if action == "start":
        result.add_argument("--scenario", choices=("economy-manager", "military-manager"))
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
        result.add_argument("--economy-interval", type=int)
        result.add_argument("--military-interval", type=int)
        result.add_argument("--review-attempts", type=int)
    return result


def resolve_options(args):
    overrides = {}
    from civ_agent.decisions_policy import DECISIONS_FIELDS

    fields = {
        "game": (
            "scenario",
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
            "economy_interval",
            "military_interval",
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
    if getattr(args, "record_replay", False):
        overrides["runtime"]["record_replay"] = True
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
    require_local_runtime(record)
    record["launchResumeRequested"] = False
    if config.agents.decisions_enabled:
        from civ_agent.decisions_ledger import DecisionsLedger

        record["decisionsAuthority"] = DecisionsLedger(auth_root).authority
    from civ_agent.decisions_backends import BACKENDS
    from civ_agent.decisions_tasks import TASK_VERSION

    record.update(
        images=images,
        auth_root=str(auth_root),
        native_setup=validation,
        experiments={
            "native_advice_enabled": config.agents.native_advice_enabled,
            "decisions_enabled": config.agents.decisions_enabled,
            "nativeGuideVersion": "native-guidance-v1",
            "classifierModel": BACKENDS[config.agents.decisions_backend].model,
            "decisionsBackend": config.agents.decisions_backend,
            "acceptanceProbability": config.agents.decisions_acceptance_probability,
            "routingPolicyVersion": config.agents.decisions_routing_policy,
            "advisoryPolicyVersion": TASK_VERSION,
        },
    )
    write_private_json(root / "container.json", record)
    write_private_json(root / "resolved.json", config.model_dump(mode="json"))
    if original is not None:
        write_private(root / "input.toml", original.read_bytes())
    return record


def resume_configuration(root, args):
    from civ_agent.orchestration.managed_host import validate_limit_increase

    config = ResolvedGameConfig.model_validate(read_json(root / "resolved.json"))
    previous_limits = config.limits.model_copy(deep=True)
    record = identity(root)
    require_local_runtime(record)
    record["launchResumeRequested"] = True
    for key in ("max_turns", "max_invocations", "max_seconds", "turn_timeout", "startup_timeout"):
        value = getattr(args, key, None)
        if value is not None:
            setattr(config.limits, key, value)
    config = ResolvedGameConfig.model_validate(config.model_dump())
    validate_limit_increase(previous_limits, config.limits)
    if args.port is not None:
        config.runtime.browser_port = args.port
    if getattr(args, "record_replay", False):
        config.runtime.record_replay = True
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
    write_private_json(root / "resolved.json", config.model_dump(mode="json"))
    return config


def stop_game(root, *, role="game"):
    record = read_json(root / "container.json")
    if record is None:
        raise ContainerError("No containerized run at this path")
    physical = deployment_identity(record).id
    write_private_json(root / "control/request.json", {"action": "stop"})
    matches = []
    if role == "batch":
        for row in read_json(root / "batch.json")["runs"]:
            match = root / "games" / row["id"]
            if (match / "container.json").exists():
                matches.append(match)
                # Publish every request before waiting, including when the batch
                # supervisor is gone and games are still draining independently.
                write_private_json(match / "control/request.json", {"action": "stop"})
    result = command(["systemctl", "--user", "stop", f"civ-{role}-{physical}"], check=False, timeout=250)
    if role == "batch" and matches:
        # Each game owns independent infrastructure and its own supervisor lock.
        # Keep fallback waits and container cleanup from accumulating serially.
        with ThreadPoolExecutor(max_workers=min(8, len(matches)), thread_name_prefix="batch-stop") as worker:
            pending = [worker.submit(stop_game, match) for match in matches]
            for future in pending:
                future.result()
    elif role != "batch" and result.returncode:
        # A batch owns its games in the batch supervisor, not separate systemd
        # units. Let their controller consume the stop request before removing
        # orphaned infrastructure. No model work is polled or restarted here.
        deadline = time.monotonic() + 120
        while True:
            try:
                with locked(root / ".supervisor.lock"):
                    cleanup(physical)
                break
            except LockBusy:
                if time.monotonic() >= deadline:
                    cleanup(physical)
                    break
                time.sleep(0.2)


def run(args):
    config = None
    try:
        os.umask(0o077)
        root = args.state_dir.expanduser().resolve()
        if args.action == "status":
            from civ_agent.experiment_status import experiment_status
            from civ_agent.operator_state import is_local, projection
            from civ_agent.operator_status import GameStatusEnvelope

            status = read_json(root / "container-status.json", {})
            local = is_local(root)
            native = projection(root)
            saved = read_json(root / "resolved.json")
            roster = ResolvedGameConfig.model_validate(saved) if saved else None
            print(
                json.dumps(
                    GameStatusEnvelope.model_validate(
                        {
                            "version": 1,
                            "containers": status,
                            "browser": read_json(root / "browser.json"),
                            "runtime_status": native.status.model_dump(mode="json")
                            if native.status is not None
                            else None,
                            "runtime_result": native.completion.model_dump(mode="json")
                            if native.completion is not None
                            else None,
                            "result": native.result.model_dump(mode="json") if native.result is not None else None,
                            "result_updated_at": native.result_updated_at,
                            "read_only_archive": not local,
                            "players": roster.player_records() if roster else [],
                            "experiments": read_json(root / "container.json", {}).get("experiments")
                            if local
                            else experiment_status(root, roster, read_json(root / "container.json", {})),
                        }
                    ).model_dump(mode="json", exclude_unset=True),
                    indent=2,
                )
            )
            return 0
        if args.action in {"stop", "remove"}:
            stop_game(root)
            if args.action == "remove":
                cleanup(deployment_identity(identity(root)).id)
            print(f"Stopped. Durable state retained: {root}")
            return 0
        if args.action == "start":
            config = resolve_options(args)
            if args.dry_run:
                native = validate_setup(config, resolve_image(config.runtime.image))
                print(
                    json.dumps(
                        {
                            "config": config.model_dump(mode="json"),
                            "turn_order": [p.civilization for p in config.turn_order],
                            "native_setup": native,
                        },
                        indent=2,
                    )
                )
                return 0
        if args.action == "resume":
            require_local_runtime(read_json(root / "container.json", {}))
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
