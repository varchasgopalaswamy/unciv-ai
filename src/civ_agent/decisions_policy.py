"""Operator-owned bounds shared by configuration, services and the trusted broker."""

import argparse
import json
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from civ_agent.decisions_backends import BACKENDS, DEFAULT_BACKEND, DecisionBackend
from civ_agent.effort_decisions import EFFORT_LEVELS, Effort, canonical_levels

MAX_DECISIONS_TIMEOUT_SECONDS = 300
EARLY_REVIEW_MIN_TURN = 10


class DecisionsSettings(BaseModel):
    model_config = ConfigDict(extra="forbid", validate_default=True, allow_inf_nan=False)

    decisions_enabled: bool = False
    decisions_backend: DecisionBackend = DEFAULT_BACKEND
    decisions_effort_enabled: bool = False
    decisions_effort_levels: Annotated[list[Effort], Field(min_length=2, max_length=5)] = Field(
        default_factory=lambda: list(EFFORT_LEVELS)
    )
    decisions_effort_min_probability: Annotated[float, Field(ge=0, le=1, strict=True)] = 0.50
    decisions_effort_min_margin: Annotated[float, Field(ge=0, le=1, strict=True)] = 0.15
    additional_decisions_budget_usd: Annotated[float, Field(ge=0, le=1000)] = 0.10
    decisions_acceptance_probability: Annotated[float, Field(ge=0, le=1, strict=True)] = Field(
        default_factory=lambda data: BACKENDS[data["decisions_backend"]].acceptance_probability
    )
    decisions_routing_policy: Literal["diplomacy-routing-v4"] = "diplomacy-routing-v4"
    decisions_incoming_batch_probability: Annotated[float, Field(ge=0, le=1, strict=True)] = Field(
        default_factory=lambda data: data["decisions_acceptance_probability"]
    )
    decisions_incoming_suppress_probability: Annotated[float, Field(ge=0, le=1, strict=True)] = Field(
        default_factory=lambda data: data["decisions_acceptance_probability"]
    )
    decisions_outgoing_batch_probability: Annotated[float, Field(ge=0, le=1, strict=True)] = Field(
        default_factory=lambda data: data["decisions_acceptance_probability"]
    )
    decisions_outgoing_omit_probability: Annotated[float, Field(ge=0, le=1, strict=True)] = Field(
        default_factory=lambda data: data["decisions_acceptance_probability"]
    )
    decisions_timeout_seconds: Annotated[int, Field(ge=1, le=MAX_DECISIONS_TIMEOUT_SECONDS)] = Field(
        default_factory=lambda data: 120 if data["decisions_backend"] == "jev" else 5
    )
    decisions_startup_min_gap_turns: Annotated[int, Field(ge=1)] = 5
    decisions_max_request_bytes: Annotated[int, Field(ge=1024, le=65536)] = 65536
    decisions_max_questions: Annotated[int, Field(ge=1, le=64)] = 64
    decisions_memory_candidate_limit: Annotated[int, Field(ge=1, le=16)] = 16
    decisions_preservation_candidate_limit: Annotated[int, Field(ge=1, le=8)] = 8
    decisions_belief_candidate_limit: Annotated[int, Field(ge=1, le=6)] = 6
    early_review_min_gap_turns: Annotated[int, Field(ge=3)] = 3
    early_review_min_gap_between_early_turns: Annotated[int, Field(ge=10)] = 10
    memory_context_chars: Annotated[int, Field(ge=1000)] = 12000

    @field_validator("decisions_effort_levels")
    @classmethod
    def effort_levels(cls, values):
        return canonical_levels(values)

    @model_validator(mode="after")
    def effort_requires_decisions(self):
        if self.decisions_effort_enabled and not self.decisions_enabled:
            raise ValueError("decisions_effort_enabled requires decisions_enabled")
        return self


DECISIONS_FIELDS = tuple(DecisionsSettings.model_fields)


def add_decisions_arguments(parser, *, defaults=True, include_memory=True):
    for name, field in DecisionsSettings.model_fields.items():
        if name == "memory_context_chars" and not include_memory:
            continue
        options = {"default": field.default if defaults and field.default_factory is None else None}
        if name in {"decisions_enabled", "decisions_effort_enabled"}:
            options["action"] = argparse.BooleanOptionalAction
        elif name == "decisions_effort_levels":
            options["type"] = json.loads
            options["metavar"] = "JSON"
        else:
            options["type"] = (
                (int if name == "decisions_timeout_seconds" else float)
                if field.default_factory is not None
                else type(field.default)
            )
        if name == "decisions_backend":
            options["choices"] = tuple(BACKENDS)
        if name == "decisions_routing_policy":
            options["choices"] = ("diplomacy-routing-v4",)
        parser.add_argument("--" + name.replace("_", "-"), **options)


def decisions_values(source):
    values = {name: getattr(source, name) for name in DECISIONS_FIELDS if getattr(source, name) is not None}
    return DecisionsSettings.model_validate(values).model_dump()


def routing_thresholds(settings, direction):
    """Routing controls never lower confidence requirements for advisory tasks."""
    if direction == "incoming":
        return {
            "batch": settings.decisions_incoming_batch_probability,
            "no_separate_response": settings.decisions_incoming_suppress_probability,
        }
    if direction == "outgoing":
        return {
            "batch": settings.decisions_outgoing_batch_probability,
            "omit": settings.decisions_outgoing_omit_probability,
        }
    raise ValueError("Unknown routing direction")
