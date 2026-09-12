"""Explicit Codex settings shared by interactive and scheduled player launchers."""

import argparse


def player_tool_arguments():
    """Keep model jobs on the configured player gateway, with no general shell."""
    disabled = (
        "shell_tool",
        "unified_exec",
        "apps",
        "browser_use",
        "computer_use",
        "view_image",
        "sleep_tool",
        "multi_agent",
        "js_repl",
    )
    result = []
    for feature in disabled:
        result.extend(["-c", f"features.{feature}=false"])
    return [*result, "-c", 'web_search="disabled"']


def valid_setting(value):
    return (
        isinstance(value, str)
        and 1 <= len(value) <= 128
        and all(character.isprintable() and not character.isspace() for character in value)
    )


def setting_argument(value):
    if not valid_setting(value):
        raise argparse.ArgumentTypeError("must be a nonempty name of at most 128 characters without whitespace")
    return value


def add_settings_arguments(parser, *, model_help, reasoning_help):
    parser.add_argument("--model", type=setting_argument, help=model_help)
    # Codex 0.153.4 models advertise their own effort names; its ReasoningEffort
    # schema is a nonempty string, not a fixed enum. Codex validates support.
    parser.add_argument(
        "--reasoning-effort",
        "--reasoning",
        dest="reasoning_effort",
        type=setting_argument,
        metavar="LEVEL",
        help=reasoning_help,
    )
