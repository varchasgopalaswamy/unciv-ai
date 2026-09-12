"""The single command-line interface for local and remote gameplay services."""

import argparse
from collections.abc import Sequence
from importlib.metadata import version

from civ_agent import scheduler
from civ_agent.commands import agent, agent_server, history, host, inspect


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(prog="civ-agent", description=__doc__)
    result.add_argument("--version", action="version", version=f"%(prog)s {version('civ-agent')}")
    groups = result.add_subparsers(dest="command", required=True)

    host_commands = groups.add_parser("host", help="Start the game host or inspect a saved game").add_subparsers(
        required=True
    )
    start = host_commands.add_parser(
        "start", parents=[host.parser(add_help=False)], help="Build and start the HTTPS game host"
    )
    start.set_defaults(handler=host.run)
    view = host_commands.add_parser(
        "inspect", parents=[inspect.parser(add_help=False)], help="Export or open a saved game in the desktop UI"
    )
    view.set_defaults(handler=inspect.run)

    agents = groups.add_parser("agent", help="Open an interactive player session").add_subparsers(required=True)
    interactive = agents.add_parser(
        "start", parents=[agent.parser(add_help=False)], help="Launch Codex as one civilization"
    )
    interactive.set_defaults(handler=agent.run)

    servers = groups.add_parser("agent-server", help="Serve scheduled turns for one player over HTTPS").add_subparsers(
        required=True
    )
    service = servers.add_parser(
        "start", parents=[agent_server.parser(add_help=False)], help="Start a civilization's HTTPS service"
    )
    service.set_defaults(handler=agent_server.run)

    matches = groups.add_parser("match", help="Schedule turns and control a match").add_subparsers(required=True)
    descriptions = {
        "start": "Start automatic turn scheduling",
        "resume": "Resume scheduling and recover an interrupted turn",
        "status": "Inspect the host and persisted scheduler state",
        "pause": "Pause host mutations and agent work",
        "stop": "Stop scheduling and pause the match",
        "manual": "Return the host to manual player control",
    }
    for action, description in descriptions.items():
        command = matches.add_parser(action, parents=[scheduler.parser(add_help=False)], help=description)
        command.set_defaults(handler=scheduler.run, action=action)

    histories = groups.add_parser("history", help="Export recorded player conversations").add_subparsers(required=True)
    export = histories.add_parser(
        "export", parents=[history.parser(add_help=False)], help="Export existing public Codex history"
    )
    export.set_defaults(handler=history.run)
    return result


def main(argv: Sequence[str] | None = None) -> int:
    args = parser().parse_args(argv)
    return args.handler(args)
