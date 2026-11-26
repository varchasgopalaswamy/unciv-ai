"""Pure player model settings shared by native launch and game configuration."""

import argparse


def valid_setting(value):
    return (
        isinstance(value, str)
        and 1 <= len(value) <= 128
        and all(character.isprintable() and not character.isspace() for character in value)
    )


def communication_effort(gameplay_effort: str | None, configured: str | None = None) -> str | None:
    """Use a cheaper diplomacy effort unless the operator chose one explicitly.

    Preserve model-specific effort names we do not recognize: replacing an
    unknown supported value with ``medium`` could make an otherwise valid model
    fail at startup.
    """
    if configured is not None:
        return configured
    if gameplay_effort in {"high", "xhigh", "max"}:
        return "medium"
    return gameplay_effort


def setting_argument(value):
    if not valid_setting(value):
        raise argparse.ArgumentTypeError("must be a nonempty name of at most 128 characters without whitespace")
    return value


def add_settings_arguments(parser, *, model_help, reasoning_help):
    parser.add_argument("--model", type=setting_argument, help=model_help)
    # Parse effort names here; native provider preflight validates support before
    # any model job is admitted.
    parser.add_argument(
        "--reasoning-effort",
        "--reasoning",
        dest="reasoning_effort",
        type=setting_argument,
        metavar="LEVEL",
        help=reasoning_help,
    )
