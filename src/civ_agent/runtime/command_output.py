"""Present compact command updates without changing the program's native results."""

import copy
from collections.abc import Iterable
from typing import Any

from pydantic_ai.messages import ToolReturnPart

from civ_agent.decision_contracts import READS
from civ_agent.orchestration.contracts import canonical
from civ_agent.response_compaction import expand_response

# Keep the current completion/authority basis directly readable alongside the
# exact delta. Other overview sections remain available in the program's full
# result, and through an explicit player_turn read.
CURRENT_FIELDS = frozenset(
    {
        "stateId",
        "turn",
        "revision",
        "phase",
        "canEndTurn",
        "pending",
        "roleCompletion",
        "militaryCompletion",
        "cityScope",
        "economyCoordination",
    }
)


def _key(value):
    try:
        return canonical(value)
    except (TypeError, ValueError):
        return None


def compact_command_output(output: Any, tool_returns: Iterable[ToolReturnPart]) -> Any:
    """Replace exact returned command results with their native compact updates.

    Only trusted child metadata supplies replacements. Explicit reads, authored
    projections, failed commands and recovered observations are left alone.
    Neither the REPL variables nor the retained child returns are mutated.
    """
    replacements = {}
    for returned in tool_returns:
        if (
            returned.tool_name not in {"act", "execute_commands", "construction_intent"}
            or returned.outcome != "success"
        ):
            continue
        metadata = returned.metadata or {}
        original = metadata.get("raw_player_observation")
        if not isinstance(original, dict) or metadata.get("observation_recoveries"):
            continue
        if not isinstance(returned.content, dict) or original.get("decisionId") != returned.content.get("decisionId"):
            continue
        originals = {row.get("operationId"): row for row in original.get("receipts", [])}
        for row in returned.content.get("receipts", []):
            if row.get("tool") in READS or row.get("status") != "confirmed":
                continue
            source = originals.get(row.get("operationId"))
            if source is None or any(source.get(key) != row.get(key) for key in ("tool", "status")):
                continue
            full, raw = row.get("result"), source.get("result")
            if not isinstance(full, dict) or not isinstance(raw, dict):
                continue
            if "error" in full or "observationRecovery" in full:
                continue
            raw = expand_response(raw)
            state_delta = raw.get("stateUpdate", {}).get("kind") in {"delta", "unchanged"}
            coordination_delta = raw.get("coordinationUpdate", {}).get("kind") in {"delta", "unchanged"}
            if not state_delta and not coordination_delta:
                continue
            fields = CURRENT_FIELDS - {"economyCoordination"} if coordination_delta else CURRENT_FIELDS
            compact = {**raw, **{key: copy.deepcopy(full[key]) for key in fields if key in full}}
            key = _key(full)
            candidate = _key(compact)
            if key is None or candidate is None or len(candidate) >= len(key):
                continue
            if key in replacements and _key(replacements[key]) != candidate:
                replacements[key] = None  # An ambiguous source has no replacement.
            else:
                replacements[key] = compact

    def project(value):
        if isinstance(value, dict):
            key = _key(value)
            replacement = replacements.get(key) if key is not None else None
            if replacement is not None:
                return copy.deepcopy(replacement)
            return {key: project(item) for key, item in value.items()}
        if isinstance(value, list):
            return [project(item) for item in value]
        return value

    return project(output) if replacements else output
