"""Operator-owned bounds shared by configuration, services and the trusted broker."""

import argparse
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

ROUTING_CONFIDENCE_FIELDS = (
    "decisions_incoming_batch_confidence",
    "decisions_incoming_suppress_confidence",
    "decisions_outgoing_batch_confidence",
    "decisions_outgoing_omit_confidence",
)


class DecisionsSettings(BaseModel):
    model_config = ConfigDict(extra="forbid", validate_default=True, allow_inf_nan=False)

    decisions_enabled: bool = False
    additional_decisions_budget_usd: Annotated[float, Field(ge=0, le=1000)] = 0.10
    decisions_min_confidence: Annotated[float, Field(ge=0, le=1)] = 0.90
    decisions_routing_policy: Literal["diplomacy-routing-v1", "diplomacy-routing-v2", "diplomacy-routing-v3"] = (
        "diplomacy-routing-v1"
    )
    decisions_incoming_batch_confidence: Annotated[float, Field(ge=0, le=1)] = 0.90
    decisions_incoming_suppress_confidence: Annotated[float, Field(ge=0, le=1)] = 0.90
    decisions_outgoing_batch_confidence: Annotated[float, Field(ge=0, le=1)] = 0.90
    decisions_outgoing_omit_confidence: Annotated[float, Field(ge=0, le=1)] = 0.90
    decisions_timeout_seconds: Annotated[int, Field(ge=1, le=5)] = 5
    decisions_startup_min_gap_turns: Annotated[int, Field(ge=1)] = 5
    decisions_max_request_bytes: Annotated[int, Field(ge=1024, le=65536)] = 65536
    decisions_max_questions: Annotated[int, Field(ge=1, le=64)] = 64
    decisions_memory_candidate_limit: Annotated[int, Field(ge=1, le=16)] = 16
    decisions_preservation_candidate_limit: Annotated[int, Field(ge=1, le=8)] = 8
    decisions_belief_candidate_limit: Annotated[int, Field(ge=1, le=6)] = 6
    early_review_min_gap_turns: Annotated[int, Field(ge=3)] = 3
    early_review_min_gap_between_early_turns: Annotated[int, Field(ge=10)] = 10
    memory_context_chars: Annotated[int, Field(ge=1000)] = 12000

    @model_validator(mode="before")
    @classmethod
    def reject_retired(cls, value):
        if isinstance(value, dict) and "diplomacy_routing_enabled" in value:
            raise ValueError("diplomacy_routing_enabled is retired; explicitly choose decisions_enabled instead")
        if isinstance(value, dict) and "decisions_routing_policy" not in value and "decisions_min_confidence" in value:
            # Older saved settings used one threshold for everything. Preserve
            # their effective routing policy when adding the separate controls.
            value = dict(value)
            for name in ROUTING_CONFIDENCE_FIELDS:
                value.setdefault(name, value["decisions_min_confidence"])
        return value


DECISIONS_FIELDS = tuple(DecisionsSettings.model_fields)


class RetiredDecisionsFlag(argparse.Action):
    def __call__(self, parser, namespace, values, option_string=None):
        parser.error(f"{option_string} is retired; explicitly choose --decisions-enabled or --no-decisions-enabled")


def add_decisions_arguments(parser, *, defaults=True, include_memory=True):
    parser.add_argument(
        "--diplomacy-routing-enabled",
        "--no-diplomacy-routing-enabled",
        action=RetiredDecisionsFlag,
        nargs=0,
        help=argparse.SUPPRESS,
    )
    for name, field in DecisionsSettings.model_fields.items():
        if name == "memory_context_chars" and not include_memory:
            continue
        options = {"default": field.default if defaults else None}
        if name == "decisions_enabled":
            options["action"] = argparse.BooleanOptionalAction
        else:
            options["type"] = type(field.default)
        if name == "decisions_routing_policy":
            options["choices"] = ("diplomacy-routing-v1", "diplomacy-routing-v2", "diplomacy-routing-v3")
        parser.add_argument("--" + name.replace("_", "-"), **options)


def decisions_values(source):
    return {name: getattr(source, name) for name in DECISIONS_FIELDS}


def routing_thresholds(settings, direction):
    """Routing controls never lower confidence requirements for advisory tasks."""
    if direction == "incoming":
        return {
            "batch": settings.decisions_incoming_batch_confidence,
            "no_separate_response": settings.decisions_incoming_suppress_confidence,
        }
    if direction == "outgoing":
        return {
            "batch": settings.decisions_outgoing_batch_confidence,
            "omit": settings.decisions_outgoing_omit_confidence,
        }
    raise ValueError("Unknown routing direction")
