"""Build and run a local, checkpointed Rome/Greece host in this terminal."""

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
    parser.add_argument("--seed", type=seed_number, help="map seed for a new game only")
    parser.add_argument(
        "--scenario",
        choices=["city-management"],
        help="required for a new game; on resume, verify that the checkpoint contains this scenario",
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


def ensure_players(path, resuming):
    """Create credentials exclusively; existing credentials are never changed."""
    if not path.exists() and not path.is_symlink():
        if resuming:
            raise ValueError(
                f"Checkpoint exists but player credentials are missing at {path}. "
                "Restore players.json or select the original file with --players."
            )
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        rome_token = secrets.token_urlsafe(32)
        greece_token = secrets.token_urlsafe(32)
        while greece_token == rome_token:
            greece_token = secrets.token_urlsafe(32)
        players = {
            "players": [
                {"civilizationId": "Rome", "token": rome_token},
                {"civilizationId": "Greece", "token": greece_token},
            ]
        }
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
    if not resuming and options.scenario is None:
        raise ValueError("No checkpoint found. Start the exercise with --scenario city-management.")
    if bool(options.tls_config) != bool(options.ca_file):
        raise ValueError("--tls-config and --ca-file must be supplied together.")
    if options.tls_config and (options.listen_host or options.public_host):
        raise ValueError("A custom TLS configuration already supplies listenHost and publicHost.")

    ensure_players(players, resuming)
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
    if options.unciv_dir:
        engine = Path(options.unciv_dir).expanduser()
        if not engine.is_absolute():
            engine = repo_dir / engine
        build_command.append(f"-PuncivDir={engine.resolve()}")
    subprocess.run(build_command, cwd=repo_dir, check=True)

    executable = repo_dir / "build/install/unciv-ai/bin/unciv-ai"
    command = [str(executable)]
    if resuming:
        command.extend(["--resume-checkpoint", str(checkpoint)])
    else:
        command.extend(["--new-duel", "--checkpoint", str(checkpoint)])
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
    action = "Resuming" if resuming else f"Starting {options.scenario}"
    print(f"{action} game: {checkpoint}", flush=True)
    print(f"Wait for MCP-READY:{origin}/mcp, then launch each agent in another terminal.", flush=True)
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
