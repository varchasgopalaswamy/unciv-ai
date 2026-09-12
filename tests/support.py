"""Shared subprocess commands for exercising the installed application."""

import sys

from civ_agent.cli import main


def match_arguments(*args: str) -> list[str]:
    action = args[0] if args and not args[0].startswith("-") else "start"
    options = args[1:] if args and args[0] == action else args
    return ["match", action, *options]


def match_command(*args: str) -> list[str]:
    return [sys.executable, "-B", "-m", "civ_agent", *match_arguments(*args)]


def run_match(args: list[str]) -> int:
    return main(match_arguments(*args))
