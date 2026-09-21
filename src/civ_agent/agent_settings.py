"""Explicit Codex settings shared by interactive and scheduled player launchers."""

import argparse
import json

from civ_agent.mcp_results import RESULT_FORMAT_HEADER, STRUCTURED_RESULT_FORMAT


def player_mcp_configuration(url: str, *, tool_timeout_seconds: int | None = None) -> str:
    """Configure Codex's player gateway with single-payload results, including resume."""
    fields = [
        "url=" + json.dumps(url),
        'bearer_token_env_var="UNCIV_PLAYER_TOKEN"',
        "required=true",
        'default_tools_approval_mode="approve"',
        "http_headers={" + json.dumps(RESULT_FORMAT_HEADER) + "=" + json.dumps(STRUCTURED_RESULT_FORMAT) + "}",
    ]
    if tool_timeout_seconds is not None:
        fields.append("tool_timeout_sec=" + str(tool_timeout_seconds))
    return "mcp_servers.civ={" + ",".join(fields) + "}"


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


def scheduled_prompt_arguments():
    """Keep unrelated, machine-dependent context out of headless player prompts.

    These controls affect prompt presentation, not sandbox enforcement. Each job
    still has its own workspace, gateway credentials and approvalPolicy=never.
    Interactive Codex sessions keep their normal environment instructions.
    """
    settings = (
        "include_environment_context=false",
        "include_permissions_instructions=false",
        "include_apps_instructions=false",
        "include_collaboration_mode_instructions=false",
        "skills.include_instructions=false",
        "features.plugins=false",
        "project_doc_max_bytes=0",
    )
    return [argument for setting in settings for argument in ("-c", setting)]


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
