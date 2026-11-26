"""Drive credential-free fixtures through the same Code Mode catalogue as models."""

import inspect
import re
from dataclasses import replace

from pydantic import TypeAdapter
from pydantic_ai.messages import ModelMessage, ModelRequest, ModelResponse, ToolCallPart, ToolReturnPart
from pydantic_ai.models.function import FunctionModel
from pydantic_ai.tools import ToolDefinition

from civ_agent.runtime.code_mode import python_name

_CALL = TypeAdapter(ToolCallPart)
_RETURN = TypeAdapter(ToolReturnPart)


def direct_child_history(messages: list[ModelMessage]) -> list[ModelMessage]:
    """Project proven sandbox children for the existing deterministic fixture."""
    flattened: list[ModelMessage] = []
    for message in messages:
        if isinstance(message, ModelResponse):
            parts = [
                part for part in message.parts if not isinstance(part, ToolCallPart) or part.tool_name != "run_code"
            ]
            if parts:
                flattened.append(replace(message, parts=parts))
            continue
        if not isinstance(message, ModelRequest):
            flattened.append(message)
            continue
        parts = []
        for part in message.parts:
            if not isinstance(part, ToolReturnPart) or part.tool_name != "run_code":
                parts.append(part)
                continue
            if part.outcome != "success" or not isinstance(part.metadata, dict):
                continue
            calls, returns = part.metadata.get("tool_calls", {}), part.metadata.get("tool_returns", {})
            for identifier, value in calls.items():
                if identifier not in returns:
                    continue
                child, result = _CALL.validate_python(value), _RETURN.validate_python(returns[identifier])
                if child.tool_call_id != result.tool_call_id or child.tool_call_id != identifier:
                    raise ValueError("SCRIPTED_CODE_CHILD_IDENTITY_CONFLICT")
                flattened.extend([ModelResponse(parts=[child]), ModelRequest(parts=[result])])
        if parts:
            flattened.append(replace(message, parts=parts))
    return flattened


def adapt_code_mode(base: FunctionModel) -> FunctionModel:
    """Translate fixture decisions, without bypassing the real sandbox or tools."""
    if base.function is None:
        raise ValueError("SCRIPTED_CODE_FUNCTION_REQUIRED")

    async def respond(messages, info):
        description = next((tool.description or "" for tool in info.function_tools if tool.name == "run_code"), "")
        signatures = {
            name: bool(asynchronous) for asynchronous, name in re.findall(r"(async )?def (\w+)\(", description)
        }
        if not signatures:
            raise ValueError("SCRIPTED_CODE_CATALOGUE_MISSING")
        direct_info = replace(info, function_tools=[ToolDefinition(name=name) for name in signatures])
        result = base.function(direct_child_history(messages), direct_info)
        if inspect.isawaitable(result):
            result = await result
        if not isinstance(result, ModelResponse):
            raise TypeError("SCRIPTED_CODE_RESPONSE_REQUIRED")
        output_names = {tool.name for tool in info.output_tools}
        parts = []
        for part in result.parts:
            if not isinstance(part, ToolCallPart) or part.tool_name in output_names:
                parts.append(part)
                continue
            arguments = part.args_as_dict()
            if "arguments" in arguments and set(arguments) <= {"arguments", "recoverBaseline"}:
                arguments = {
                    **arguments["arguments"],
                    **{key: value for key, value in arguments.items() if key != "arguments"},
                }
            name = python_name(part.tool_name)
            if name not in signatures:
                raise ValueError("SCRIPTED_CODE_TOOL_UNAVAILABLE: " + name)
            code = (
                ("await " if signatures[name] else "")
                + name
                + "("
                + ", ".join(key + "=" + repr(value) for key, value in arguments.items())
                + ")"
            )
            parts.append(ToolCallPart("run_code", {"code": code}, "monty-" + part.tool_call_id))
        return replace(result, parts=parts)

    return FunctionModel(respond, model_name=base.model_name)
