"""Explicit Codex settings shared by interactive and scheduled player launchers."""

import argparse


TURN_REVIEW_PROMPT = (
    "Before END_TURN, read turnReview: compare confirmed actions and remaining movement with your plan. "
    "Act or deliberately defer; send reviewId=turnReview.id and deferReason explaining idle units with legalDestinations>0. "
    "Read previousTurnReview for earlier deferred actions. "
)

CLOSE_START_PROMPT = (
    "Only when player_turn.scenario.name is CLOSE_START_DIPLOMACY, exercise war and peace once. "
    "Follow scenario.phase: AWAITING_WAR: Rome declares war on Greece when legal after reading "
    "declarationConfirmation, with confirmWar=true; Greece waits. AT_WAR: Rome proposes peace when negotiatePeace.available, "
    "using only the offered Peace Treaty in both peaceTerms.give and peaceTerms.receive; "
    "Greece reads and accepts that incoming offer when legal. Continue ordinary turns while waiting. "
    "COMPLETE: never redeclare for this exercise, even after treaty expiry. Record confirmed progress or the next step in your summary. "
)


def valid_setting(value):
    return isinstance(value, str) and 1 <= len(value) <= 128 and all(
        character.isprintable() and not character.isspace() for character in value
    )


def setting_argument(value):
    if not valid_setting(value):
        raise argparse.ArgumentTypeError("must be a nonempty name of at most 128 characters without whitespace")
    return value


def add_settings_arguments(parser, *, model_help, reasoning_help):
    parser.add_argument("--model", type=setting_argument, help=model_help)
    # Codex 0.153.4 models advertise their own effort names; its ReasoningEffort
    # schema is a nonempty string, not a fixed enum. Codex validates support.
    parser.add_argument("--reasoning-effort", "--reasoning", dest="reasoning_effort",
                        type=setting_argument, metavar="LEVEL", help=reasoning_help)
