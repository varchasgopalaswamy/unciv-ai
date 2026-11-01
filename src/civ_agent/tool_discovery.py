"""Shared startup guidance for tool discovery and private decision records."""

from typing import Literal


def tool_discovery_instructions(purpose: Literal["gameplay", "economy", "military", "communication"]) -> str:
    from civ_agent.decision_contracts import DECISION_INSTRUCTIONS

    return DECISION_INSTRUCTIONS + " Follow error recovery instructions; stop on recoverable=false."
