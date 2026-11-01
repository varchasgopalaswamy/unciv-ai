"""Open an interactive Codex player using the existing local host credentials."""

import argparse
import contextlib
import json
import os
import re
import shlex
import shutil
import stat
import subprocess
import sys
from pathlib import Path

from civ_agent.agent_settings import add_settings_arguments, player_mcp_configuration, player_tool_arguments
from civ_agent.player_prompts import GAMEPLAY_WORKFLOW
from civ_agent.tls import (
    TLSConfigError,
    codex_trust_environment,
    endpoint_configuration,
    private_directory,
    verify_https_connection,
)
from civ_agent.turn_archive import civilization_directory_name


class LaunchError(Exception):
    pass


def port_number(value):
    try:
        port = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError("port must be an integer from 1 to 65535") from None
    if not 1 <= port <= 65535:
        raise argparse.ArgumentTypeError("port must be an integer from 1 to 65535")
    return port


def player_credentials(path, requested):
    try:
        with path.open("rb") as source:
            mode = os.fstat(source.fileno()).st_mode
            if not stat.S_ISREG(mode) or stat.S_IMODE(mode) & ~0o600:
                raise LaunchError("Player credentials must be a regular file with owner-only permissions (chmod 600).")
            content = source.read(65537)
        if len(content) > 65536:
            raise LaunchError("Player credentials exceed 64 KiB.")
        config = json.loads(content)
        if not isinstance(config, dict) or set(config) != {"players"}:
            raise ValueError()
        players = config["players"]
        if not isinstance(players, list) or not 1 <= len(players) <= 64:
            raise ValueError()
        names, tokens = set(), set()
        for player in players:
            if (
                not isinstance(player, dict)
                or not {"civilizationId", "token"} <= set(player)
                or not set(player) <= {"civilizationId", "token", "controller"}
                or player.get("controller", "AGENT") not in {"HUMAN", "AGENT"}
            ):
                raise ValueError()
            name, token = player["civilizationId"], player["token"]
            if not isinstance(name, str) or not name.strip():
                raise ValueError()
            if not isinstance(token, str) or not re.fullmatch(r"[A-Za-z0-9_-]{43}", token):
                raise ValueError()
            if name in names or token in tokens:
                raise ValueError()
            names.add(name)
            tokens.add(token)
    except FileNotFoundError:
        raise LaunchError("No player credentials found. Run civ-agent host start first, or supply --players.") from None
    except (ValueError, UnicodeError, TypeError):
        # JSON exceptions may quote credentials. Never include their text or cause.
        raise LaunchError("Invalid player credentials file.") from None
    matches = [p for p in players if p["civilizationId"].casefold() == requested.casefold()]
    if len(matches) != 1:
        raise LaunchError("Civilization is missing or ambiguous in the player credentials file.")
    return matches[0]["civilizationId"], matches[0]["token"]


def workspace_name(civilization):
    try:
        return civilization_directory_name(civilization)
    except ValueError:
        raise LaunchError("Civilization name cannot be represented by a player directory.") from None


def initial_prompt(civilization):
    from civ_agent.scheduler import player_prompt

    return (
        GAMEPLAY_WORKFLOW
        + "\n\n"
        + player_prompt(civilization)
        + (
            "Use memory_read/history_read for earlier evidence. After completing one turn, report the outcome to the human."
        )
    )


def run_codex(executable, command, environment):
    return subprocess.call(command, executable=executable, env=environment)


@contextlib.contextmanager
def proposal_session(state_dir, civilization, token, endpoint, args):
    from civ_agent.scheduler import agent_services

    services = agent_services(state_dir)
    if civilization not in services:
        raise LaunchError("Start civ-agent agent-server start for this civilization first.")
    service = services[civilization]
    job = service.call(
        "open_interactive", playerToken=token, hostUrl=endpoint, model=args.model, reasoningEffort=args.reasoning_effort
    )
    try:
        yield job
    finally:
        service.call("close_interactive", jobId=job["jobId"])


def parser(*, add_help: bool = True) -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__, add_help=add_help)
    result.add_argument("civilization", help="Civilization name, for example Rome or Greece")
    result.add_argument(
        "--state-dir",
        type=Path,
        default=Path(os.environ.get("UNCIV_HOST_DIR", Path.home() / ".local/state/unciv-host/demo")),
    )
    result.add_argument("--players", type=Path, help="Credentials file (default: STATE_DIR/players.json)")
    result.add_argument("--port", type=port_number, help="Override local HTTPS host port")
    result.add_argument("--url", help="HTTPS host origin or MCP URL; defaults to STATE_DIR/endpoints.json")
    result.add_argument("--ca-file", type=Path, help="CA PEM bundle; defaults to STATE_DIR/endpoints.json")
    add_settings_arguments(
        result,
        model_help="Optional Codex model override",
        reasoning_help="Optional Codex reasoning effort override, e.g. high",
    )
    result.add_argument("--resume", metavar="SESSION_ID", help="Resume an explicit Codex conversation ID")
    result.add_argument("--prompt", help="Replace the default one-turn starting request")
    result.add_argument(
        "--dry-run", action="store_true", help="Show the command without launching Codex or printing tokens"
    )
    return result


def run(args: argparse.Namespace) -> int:
    from civ_agent.scheduler import RunnerError

    try:
        state_dir = args.state_dir.expanduser().resolve()
        players_file = (args.players.expanduser() if args.players else state_dir / "players.json").resolve()
        civilization, token = player_credentials(players_file, args.civilization)
        codex = shutil.which("codex")
        if codex is None:
            raise LaunchError("Codex CLI is not on PATH. Install it and log in before launching a player.")
        workspace = state_dir / "agents" / workspace_name(civilization)
        origin, ca_file = endpoint_configuration(state_dir, args.url, args.ca_file, args.port)
        endpoint = origin + "/mcp"
        # A per-invocation override; never write ~/.codex/config.toml or put a token in argv.
        configuration = player_mcp_configuration(endpoint)
        command = [codex, "--cd", str(workspace), "-c", configuration, *player_tool_arguments()]
        if args.model:
            command += ["--model", args.model]
        if args.reasoning_effort is not None:
            command += ["-c", "model_reasoning_effort=" + json.dumps(args.reasoning_effort)]
        if args.resume:
            command += ["resume", "--", args.resume]
        else:
            command += ["--"]
        prompt = args.prompt if args.prompt is not None else initial_prompt(civilization)
        command += [
            "Civ action guidance: Use the available player tools. Read required previews and ownership "
            "before acting; inspect receipts and follow recovery instructions. Social promises record "
            "intent and never prohibit native-legal actions.\n\n" + prompt
        ]
        if args.dry_run:
            print(f"Player: {civilization}; token supplied through UNCIV_PLAYER_TOKEN (not shown).")
            print(f"HTTPS trust: {ca_file or 'system certificate authorities'}")
            print(shlex.join(command))
            return 0
        try:
            verify_https_connection(endpoint, ca_file)
        except OSError:
            raise LaunchError(
                "Could not verify the HTTPS host connection. Check its certificate and CA file; run civ-agent host start and wait for MCP-READY first."
            ) from None
        os.umask(0o077)
        private_directory(state_dir / "agents")
        private_directory(workspace)
        with proposal_session(state_dir, civilization, token, endpoint, args) as job:
            gateway_ca = Path(job["caFile"]) if job["caFile"] else None
            environment = codex_trust_environment(gateway_ca, os.environ, workspace)
            for name in list(environment):
                if name.startswith("UNCIV_"):
                    del environment[name]
            environment["UNCIV_PLAYER_TOKEN"] = job["token"]
            command[command.index("-c") + 1] = player_mcp_configuration(job["url"])
            if job["context"]:
                command[-1] += "\n\n" + job["context"]
            print(f"Launching Codex as {civilization} through its HTTPS proposal gateway", flush=True)
            print(f"Player working directory: {workspace}", flush=True)
            return run_codex(codex, command, environment)
    except (LaunchError, TLSConfigError, OSError, RunnerError) as failure:
        print(f"Error: {failure}", file=sys.stderr)
        return 2
