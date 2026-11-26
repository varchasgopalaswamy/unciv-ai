"""Compact model-facing results while preserving complete code and history data."""

import copy
from collections.abc import Iterable, Mapping
from typing import Any

from pydantic_ai.messages import ToolCallPart, ToolReturnPart

from civ_agent.orchestration.contracts import canonical
from civ_agent.runtime.command_output import compact_command_output
from civ_agent.shared_values import compact_shared_values, expand_shared_values, result_copies
from civ_agent.state_updates import same_json


def share_output_values(value: Any) -> Any:
    """Share exact repeated payloads only within documented result shapes."""
    if isinstance(value, dict):
        if result_copies(value):
            # An authored collision is not our registry. Leave it intact rather
            # than validating, expanding or reinterpreting its content.
            if "sharedValues" in value:
                return value
            try:
                canonical(value)
            except (TypeError, ValueError):
                return value
            return compact_shared_values(value)
        return {key: share_output_values(item) for key, item in value.items()}
    if isinstance(value, list):
        return [share_output_values(item) for item in value]
    return value


def expand_output_values(value: Any) -> Any:
    """Reference inverse of model-facing sharing; never creates observation credit."""
    if isinstance(value, dict):
        if result_copies(value) and "sharedValues" in value:
            return expand_shared_values(value)
        return {key: expand_output_values(item) for key, item in value.items()}
    if isinstance(value, list):
        return [expand_output_values(item) for item in value]
    return value


def compact_tool_output(
    output: Any, tool_returns: Iterable[ToolReturnPart], tool_calls: Mapping[str, ToolCallPart]
) -> Any:
    returns = list(tool_returns)
    replacements = []
    for returned in returns:
        if returned.tool_name != "track_investment" or returned.outcome != "success":
            continue
        call = tool_calls.get(returned.tool_call_id)
        if call is None or call.tool_name != returned.tool_name or not isinstance(returned.content, dict):
            continue
        args = call.args_as_dict()
        checkpoint = args.get("checkpoint", args)
        workspace = returned.content.get("strategy")
        if not isinstance(checkpoint, dict) or not isinstance(workspace, dict):
            continue
        content = workspace.get("content")
        if not isinstance(content, dict):
            continue
        decision = next(
            (item for item in content.get("decisions", []) if item.get("id") == checkpoint.get("decisionId")), None
        )
        if decision is None:
            continue
        compact = {
            **returned.content,
            "strategy": {
                **{key: workspace[key] for key in ("id", "version") if key in workspace},
                "decision": decision,
                "read": {"tool": "review_investments", "arguments": {}},
            },
        }
        if len(canonical(compact)) >= len(canonical(returned.content)):
            continue
        replacements.append((returned.content, compact))

    def project(value):
        if isinstance(value, dict):
            try:
                matches = [compact for full, compact in replacements if same_json(value, full)]
            except (TypeError, ValueError):
                matches = []
            if matches and all(same_json(matches[0], item) for item in matches):
                return copy.deepcopy(matches[0])
            return {key: project(item) for key, item in value.items()}
        if isinstance(value, list):
            return [project(item) for item in value]
        return value

    return share_output_values(compact_command_output(project(output), returns))
