"""Build and run a checkpointed Vanilla Rome/Greece/France game or prepared exercise."""

import argparse
import json
import os
import secrets
import stat
import subprocess
import sys
from pathlib import Path

from civ_agent.checkout import repository_root
from civ_agent.tls import ensure_host_tls, host_origin, regular_file, validate_host_tls_config, write_private_json


def port_number(value):
    try:
        port = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError("port must be an integer") from None
    if not 1 <= port <= 65535:
        raise argparse.ArgumentTypeError("port must be between 1 and 65535")
    return port


def seed_number(value):
    try:
        seed = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError("seed must be an integer") from None
    if not -(2**63) <= seed < 2**63:
        raise argparse.ArgumentTypeError("seed must be a signed 64-bit integer")
    return seed


def parser(*, add_help: bool = True) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, add_help=add_help)
    parser.add_argument(
        "--state-dir",
        default=os.environ.get("UNCIV_HOST_DIR", "~/.local/state/unciv-host/demo"),
        help="match directory (default: UNCIV_HOST_DIR or ~/.local/state/unciv-host/demo)",
    )
    parser.add_argument("--port", type=port_number, default=8765)
    parser.add_argument("--checkpoint", help="checkpoint file (default: STATE_DIR/exploration.json)")
    parser.add_argument("--players", help="credentials file (default: STATE_DIR/players.json)")
    parser.add_argument("--operator-config", help="operator credentials (default: STATE_DIR/operator.json)")
    parser.add_argument("--tls-config", help="custom TLS key store configuration; requires --ca-file")
    parser.add_argument("--ca-file", help="CA PEM bundle for a custom TLS configuration")
    parser.add_argument("--listen-host", help="HTTPS bind address (default: 127.0.0.1)")
    parser.add_argument("--public-host", help="HTTPS certificate hostname (default: localhost)")
    parser.add_argument("--new", action="store_true", help="require a new game; never overwrite a checkpoint")
    parser.add_argument(
        "--city-states",
        type=int,
        choices=range(42),
        metavar="0-41",
        help="city-states in a new ordinary game (default: 0)",
    )
    parser.add_argument("--seed", type=seed_number, help="map seed for a new game only")
    parser.add_argument("--human", help="Open the local desktop for one human civilization")
    parser.add_argument(
        "--built-in-ai",
        action="append",
        default=[],
        metavar="CIVILIZATION",
        help="add a native AI major to a new ordinary game; may be repeated",
    )
    parser.add_argument(
        "--agent-player", action="append", default=[], help="Agent civilization; repeat to replace the default roster"
    )
    size = parser.add_mutually_exclusive_group()
    size.add_argument("--map-size", type=str.title, choices=("Tiny", "Small", "Medium", "Large", "Huge"))
    size.add_argument("--map-radius", type=int, choices=range(2, 501), metavar="2-500")
    parser.add_argument(
        "--trade-counteroffers",
        type=int,
        choices=range(101),
        metavar="0-100",
        help="shared counteroffer limit (default: 3; resumes saved setting)",
    )
    parser.add_argument(
        "--trade-timeout",
        type=int,
        choices=range(1, 86401),
        metavar="SECONDS",
        help="negotiation wall-clock deadline (default: 300; resumes saved setting)",
    )
    parser.add_argument(
        "--scenario",
        choices=["trade-cycle", "war-paid", "war-joint", "agreement-lifecycle", "mixed-diplomacy"],
        help="prepared exercise (default: Vanilla Rome/Greece/France); on resume, verify the saved scenario",
    )
    parser.add_argument(
        "--unciv-dir", help="engine checkout for code and assets; relative paths use the repository root"
    )
    parser.add_argument(
        "--repo-dir", type=Path, help="Game host checkout (default: discover from cwd or CIV_AGENT_REPO)"
    )
    return parser


def absolute_path(value):
    return Path(value).expanduser().absolute()


def ensure_players(path, resuming, civilizations, humans=()):
    """Create credentials exclusively; existing credentials are never changed."""
    if not path.exists() and not path.is_symlink():
        if resuming:
            raise ValueError(
                f"Checkpoint exists but player credentials are missing at {path}. "
                "Restore players.json or select the original file with --players."
            )
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        tokens = set()
        players = {"players": []}
        for civilization in civilizations:
            token = secrets.token_urlsafe(32)
            while token in tokens:
                token = secrets.token_urlsafe(32)
            tokens.add(token)
            players["players"].append(
                {
                    "civilizationId": civilization,
                    "token": token,
                    **({"controller": "HUMAN"} if civilization in humans else {}),
                }
            )
        try:
            descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            # Another launcher may have created it; never replace their credentials.
            pass
        else:
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                os.fchmod(stream.fileno(), 0o600)
                json.dump(players, stream, indent=2)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
    if not path.is_file():
        raise ValueError(f"Player credentials must be a regular file: {path}")
    if os.name == "posix" and stat.S_IMODE(path.stat().st_mode) & ~0o600:
        raise ValueError(f"Player credentials must have owner-only permissions; run chmod 600 on {path}")


def launch(options):
    repo_dir = repository_root(options.repo_dir)
    state_dir = absolute_path(options.state_dir)
    checkpoint = absolute_path(options.checkpoint) if options.checkpoint else state_dir / "exploration.json"
    players = absolute_path(options.players) if options.players else state_dir / "players.json"
    operator = absolute_path(options.operator_config) if options.operator_config else state_dir / "operator.json"
    resuming = checkpoint.exists() or checkpoint.is_symlink()
    if resuming and options.new:
        raise ValueError(f"Checkpoint already exists at {checkpoint}. Choose a different --state-dir or --checkpoint.")
    if resuming and not checkpoint.is_file():
        raise ValueError(f"Checkpoint must be a regular file: {checkpoint}")
    if resuming and options.seed is not None:
        raise ValueError("--seed applies only to a new game; this checkpoint will be resumed.")
    if options.city_states is not None and (resuming or options.scenario is not None):
        raise ValueError("--city-states applies only to a new ordinary game.")
    if options.built_in_ai and (resuming or options.scenario is not None):
        raise ValueError("--built-in-ai applies only to a new ordinary game.")
    if (options.agent_player or options.map_size or options.map_radius) and (resuming or options.scenario is not None):
        raise ValueError("Player and map options apply only to a new ordinary game.")
    agents = options.agent_player or ["Rome", "Greece", "France"]
    humans = [options.human] if options.human else []
    if resuming:
        ensure_players(players, True, ())
        saved_players = json.loads(players.read_text())["players"]
        agents = [entry["civilizationId"] for entry in saved_players if entry.get("controller", "AGENT") == "AGENT"]
        saved_humans = [entry["civilizationId"] for entry in saved_players if entry.get("controller") == "HUMAN"]
        if options.human and set(saved_humans) != set(humans):
            raise ValueError("The requested desktop player differs from the saved controller assignments.")
        if len(saved_humans) == 1 and options.scenario is None:
            options.human = saved_humans[0]
            humans = saved_humans
    if options.human and options.scenario:
        raise ValueError("--human requires an ordinary game.")
    nations = humans + agents + options.built_in_ai
    if len(set(nations)) != len(nations):
        raise ValueError("Human, agent and built-in AI civilizations must be distinct.")
    if bool(options.tls_config) != bool(options.ca_file):
        raise ValueError("--tls-config and --ca-file must be supplied together.")
    if options.tls_config and (options.listen_host or options.public_host):
        raise ValueError("A custom TLS configuration already supplies listenHost and publicHost.")

    civilizations = ("Rome", "Greece") if options.scenario == "trade-cycle" else humans + agents
    if options.scenario == "mixed-diplomacy":
        civilizations = ("Rome", "Greece", "France", "Egypt")
    ensure_players(
        players, resuming, civilizations, ("France", "Egypt") if options.scenario == "mixed-diplomacy" else humans
    )
    ensure_operator(operator, players)
    state_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    checkpoint.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if options.tls_config:
        tls_config = absolute_path(options.tls_config)
        ca_file = regular_file(absolute_path(options.ca_file))
        tls = validate_host_tls_config(tls_config)
        public_host = tls["publicHost"]
    else:
        public_host = options.public_host or "localhost"
        tls_config, ca_file = ensure_host_tls(state_dir, options.listen_host or "127.0.0.1", public_host)
    origin = host_origin(public_host, options.port)
    build_command = [str(repo_dir / "gradlew"), "installDist", "--console=plain"]
    if options.human:
        build_command.append("prepareDesktopAssets")
    engine = repo_dir / "vendor/Unciv"
    if options.unciv_dir:
        engine = Path(options.unciv_dir).expanduser()
        if not engine.is_absolute():
            engine = repo_dir / engine
        build_command.append(f"-PuncivDir={engine.resolve()}")
    subprocess.run(build_command, cwd=repo_dir, check=True)

    executable = repo_dir / "build/install/unciv-ai/bin/unciv-ai"
    command = [str(executable)]
    if options.human:
        command.extend(["--human", options.human])
    if options.trade_counteroffers is not None:
        command.extend(["--trade-counteroffers", str(options.trade_counteroffers)])
    if options.trade_timeout is not None:
        command.extend(["--trade-timeout", str(options.trade_timeout)])
    if resuming:
        command.extend(["--resume-checkpoint", str(checkpoint)])
    else:
        command.extend(["--new-game", "--checkpoint", str(checkpoint)])
        if options.city_states is not None:
            command.extend(["--city-states", str(options.city_states)])
        for civilization in options.built_in_ai:
            command.extend(["--built-in-ai", civilization])
        for civilization in options.agent_player:
            command.extend(["--agent-player", civilization])
        if options.map_size:
            command.extend(["--map-size", options.map_size])
        if options.map_radius:
            command.extend(["--map-radius", str(options.map_radius)])
        if options.seed is not None:
            command.extend(["--seed", str(options.seed)])
    if options.scenario is not None:
        command.extend(["--scenario", options.scenario])
    command.extend(
        [
            "--data-dir",
            str(state_dir / "data"),
            "--mcp-config",
            str(players),
            "--operator-config",
            str(operator),
            "--tls-config",
            str(tls_config),
            "--port",
            str(options.port),
        ]
    )
    write_private_json(state_dir / "endpoints.json", {"hostUrl": origin, "caFile": str(ca_file)})
    for variable, checkout in (
        ("UNCIV_HOST_REVISION", repo_dir),
        ("UNCIV_ENGINE_REVISION", engine),
    ):
        try:
            revision = subprocess.check_output(["git", "-C", str(checkout), "rev-parse", "HEAD"], text=True).strip()
            dirty = subprocess.check_output(["git", "-C", str(checkout), "status", "--porcelain"], text=True).strip()
            os.environ[variable] = revision + ("+dirty" if dirty else "")
        except (OSError, subprocess.CalledProcessError):
            os.environ[variable] = "unknown"
    action = "Resuming" if resuming else f"Starting {options.scenario or 'Vanilla Rome/Greece/France'}"
    print(f"{action} game: {checkpoint}", flush=True)
    print(f"Wait for MCP-READY:{origin}/mcp, then launch each agent in another terminal.", flush=True)
    if options.human:
        os.chdir(repo_dir / "build/desktop-engine/android/assets")
    os.execv(str(executable), command)


def ensure_operator(path, players):
    if path.resolve() == players.resolve():
        raise ValueError("Operator credentials must use a different file from player credentials.")
    if not path.exists() and not path.is_symlink():
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        try:
            descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            pass
        else:
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                os.fchmod(stream.fileno(), 0o600)
                json.dump({"token": secrets.token_urlsafe(32)}, stream)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
    if not path.is_file() or stat.S_IMODE(path.stat().st_mode) & ~0o600:
        raise ValueError(f"Operator credentials must be a regular owner-only file; run chmod 600 on {path}")


def run(options: argparse.Namespace) -> int:
    try:
        launch(options)
    except subprocess.CalledProcessError as failure:
        print(f"Host build failed (exit {failure.returncode}). See Gradle output above.", file=sys.stderr)
        return failure.returncode if failure.returncode > 0 else 1
    except (OSError, ValueError) as failure:
        print(f"civ-agent host start: {failure}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130
    return 0
