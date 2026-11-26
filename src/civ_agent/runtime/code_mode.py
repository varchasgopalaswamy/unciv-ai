"""Harness Code Mode with native failure fencing and lossless private outcomes."""

import asyncio
import copy
import json
import keyword
import re
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field, fields, replace
from functools import lru_cache
from typing import Any, NotRequired, cast

from pydantic import TypeAdapter, ValidationError
from pydantic_ai import Tool
from pydantic_ai.capabilities import AbstractCapability, CapabilityOrdering, HandleDeferredToolCalls
from pydantic_ai.exceptions import (
    ApprovalRequired,
    CallDeferred,
    ModelRetry,
    ToolFailed,
    ToolFailedError,
    ToolRetryError,
)
from pydantic_ai.messages import ToolReturn
from pydantic_ai.toolsets import WrapperToolset
from pydantic_ai_harness import CodeMode, ToolOutputLimits
from pydantic_ai_harness.code_mode import CodeModeToolset, RunCodeArgs
from pydantic_ai_harness.tool_output_limits import Band, Passthrough, Spill
from pydantic_core import SchemaValidator, from_json

from civ_agent.decision_contracts import READS
from civ_agent.orchestration.contracts import digest
from civ_agent.orchestration.store import JournalConflict
from civ_agent.runtime.code_mode_results import LOCAL_HELPER_SOURCE, LOCAL_HELPER_VERSION, RESULT_HELPERS
from civ_agent.schemas import argument_feedback
from civ_agent.tool_recovery import recoveries, recovery_response

_parent: ContextVar[str | None] = ContextVar("native_code_parent", default=None)
_active_fence: ContextVar[Any] = ContextVar("native_code_fence", default=None)
_CORRECTABLE = (ModelRetry, CallDeferred, ApprovalRequired, ToolFailed, ToolFailedError, ToolRetryError)
_ACTIONS = {
    "act",
    "execute_commands",
    "construction_intent",
    "economy_wait",
    "write_memory",
    "delete_memory",
    "save_city_plan",
    "delete_city_plan",
    "save_strategy",
    "choose_investment",
    "track_investment",
    "delete_strategy",
    "write_plan",
    "add_task",
    "update_task_status",
    "update_task_statuses",
    "remove_task",
    "add_subtask",
    "set_dependency",
}
_FATAL_RESULTS = {"SOURCE_EPOCH_RETIRED", "STALE_EPOCH", "STALE_AUTHORITY", "NATIVE_RECEIPT_UNCERTAIN"}


@contextmanager
def detached_code_mode_context():
    """A deferred child agent has its own program identity and fatal fence.

    The parent handler explicitly fences failures when the callback returns.
    Restoring its context afterward permits original deferred-call settlement,
    without attributing a child's native output to the parent's program.
    """
    parent = _parent.set(None)
    fence = _active_fence.set(None)
    recovery = recoveries.set(None)
    try:
        yield
    finally:
        recoveries.reset(recovery)
        _active_fence.reset(fence)
        _parent.reset(parent)


def python_name(name: str) -> str:
    """Match Harness's documented Python name conversion, rejecting collisions."""
    name = re.sub(r"[^a-zA-Z0-9_]", "_", name) or "_"
    if name[0].isdigit():
        name = "_" + name
    return name + "_" if keyword.iskeyword(name) else name


def validate_catalogue(names):
    owners = {}
    for name in names:
        safe = python_name(name)
        if safe == "run_code" or safe in owners or safe.startswith("_cm_") or safe == "_CmAny":
            raise JournalConflict("CODE_MODE_CATALOGUE_COLLISION")
        owners[safe] = name
    return owners


def truthful_parameters(schema):
    """Render union object fields without changing the canonical argument validator.

    The pinned SDK only renders top-level properties. Conditional requiredness
    remains the canonical model's job; a field required in every branch remains
    required in the Python catalogue and its first-snippet type stubs.
    """
    variants = schema.get("anyOf") or schema.get("oneOf")
    if not variants:
        return schema
    if any(variant.get("type") != "object" or "properties" not in variant for variant in variants):
        raise JournalConflict("CODE_MODE_UNRENDERABLE_ARGUMENT_SCHEMA")
    properties = {}
    required = set(variants[0].get("required", []))
    for variant in variants:
        required.intersection_update(variant.get("required", []))
        for name, shape in variant["properties"].items():
            if name not in properties:
                properties[name] = copy.deepcopy(shape)
            elif properties[name] != shape:
                previous = properties[name]
                choices = previous.get("anyOf", [previous])
                if shape not in choices:
                    choices = [*choices, copy.deepcopy(shape)]
                    defaults = [item["default"] for item in choices if "default" in item]
                    properties[name] = {
                        "anyOf": choices,
                        **(
                            {"default": defaults[0]}
                            if defaults and all(item == defaults[0] for item in defaults)
                            else {}
                        ),
                    }
    return {
        "type": "object",
        "properties": properties,
        "required": sorted(required),
        "additionalProperties": False,
        **({"$defs": copy.deepcopy(schema["$defs"])} if "$defs" in schema else {}),
    }


@dataclass
class CanonicalKeywordArguments:
    """Only unwrap Python's explicit arguments object; validate the native body."""

    validator: Any

    def validate_python(self, value, **kwargs):
        if isinstance(value, dict) and set(value) == {"arguments"}:
            value = value["arguments"]
        return self.validator.validate_python(value, **kwargs)

    def validate_json(self, value, *, allow_partial=False, **kwargs):
        try:
            parsed = from_json(value, allow_partial=allow_partial)
        except ValueError:
            return self.validator.validate_json(value, allow_partial=allow_partial, **kwargs)
        return self.validate_python(parsed, allow_partial=allow_partial, **kwargs)


def truthful_nested_schema(value, *, nested=False):
    """Keep same-model conditional object branches from losing their fields.

    The SDK interns nested TypedDicts by title, so two Intent branches called
    Intent otherwise keep only the first branch's required reason. Different
    models (commands and tagged query variants) remain distinct unions.
    """
    if isinstance(value, list):
        return [truthful_nested_schema(item, nested=True) for item in value]
    if not isinstance(value, dict):
        return value
    result = {key: truthful_nested_schema(item, nested=True) for key, item in value.items()}
    variants = result.get("anyOf") or result.get("oneOf")
    if variants and all(
        isinstance(item, dict) and item.get("type") == "object" and "properties" in item for item in variants
    ):
        titles = {item.get("title") for item in variants}
        if len(titles) == 1 and None not in titles:
            result = {**truthful_parameters(result), "title": variants[0]["title"]}
    if nested and any(keyword.iskeyword(name) or not name.isidentifier() for name in result.get("properties", {})):
        # The pinned renderer cannot write functional TypedDict syntax. A
        # dictionary with its complete canonical schema in the description is
        # truthful; an invalid class declaration is not. Execution keeps the
        # original schema validator, including every field constraint.
        return {
            "type": "object",
            "additionalProperties": True,
            "description": "Canonical object schema: " + json.dumps(result, ensure_ascii=False),
        }
    return result


def python_parameters(tool):
    schema = truthful_parameters(truthful_nested_schema(tool.tool_def.parameters_json_schema))
    reserved = {name for name in schema.get("properties", {}) if keyword.iskeyword(name) or not name.isidentifier()}
    if reserved:
        nested = copy.deepcopy(schema)
        schema = {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "arguments": {
                    "type": "object",
                    "additionalProperties": True,
                    "description": "Canonical arguments (dictionary keys may be Python keywords): "
                    + json.dumps(nested, ensure_ascii=False),
                    **({"default": {}} if not nested.get("required") else {}),
                }
            },
            "required": ["arguments"] if nested.get("required") else [],
        }
        tool = replace(tool, args_validator=CanonicalKeywordArguments(tool.args_validator))
    return replace(
        tool,
        tool_def=replace(
            tool.tool_def,
            parameters_json_schema=schema,
            return_schema=truthful_nested_schema(tool.tool_def.return_schema, nested=True),
        ),
    )


@dataclass
class CheckedCatalogue(WrapperToolset[Any]):
    required: frozenset[str] = frozenset()
    reserved: frozenset[str] = frozenset()

    async def get_tools(self, ctx):
        tools = await self.wrapped.get_tools(ctx)
        names = validate_catalogue(tools)
        if self.reserved.intersection(names):
            raise JournalConflict("CODE_MODE_CATALOGUE_COLLISION")
        if not self.required.issubset(tools):
            raise JournalConflict("CODE_MODE_CATALOGUE_INCOMPLETE")
        return {name: python_parameters(tool) for name, tool in sorted(tools.items())}


class FocusedCodeArgs(RunCodeArgs):
    focused: NotRequired[bool]


_FOCUSED_ARGS = cast(SchemaValidator, TypeAdapter(FocusedCodeArgs).validator)


@lru_cache(maxsize=4)
def local_helper_catalogue(include_context=False, include_selectors=False):
    from civ_agent.runtime.code_mode_results import (
        required_result_field,
        result_data,
        result_entity,
        result_feedback,
        result_view,
    )

    definitions = {
        function.__name__: Tool(cast(Any, function), sequential=True).tool_def
        for function in (result_data, result_feedback, result_view, required_result_field, result_entity)
    }
    if include_selectors:
        from civ_agent.runtime import code_mode_results as code_mode_selectors

        definitions.update(
            {
                name: Tool(getattr(code_mode_selectors, name), sequential=True).tool_def
                for name in code_mode_selectors.RESULT_SELECTORS
            }
        )
    if include_context:
        from civ_agent.runtime.delivered_context import delivered_context

        definitions["delivered_context"] = Tool(delivered_context, sequential=True).tool_def
    return (
        "Pure worker-local functions (no SDK tool call, observation credit or effects):\n"
        + CodeModeToolset._render_catalog(definitions)
    )


@dataclass
class LocalCodeModeToolset(CodeModeToolset[Any]):
    """Pinned Harness adapter: install trusted code in the isolated REPL itself."""

    execution_windows: Any = None
    focused_policy: Any = None
    include_context: bool = False
    include_selectors: bool = False

    async def get_tools(self, ctx):
        tools = await super().get_tools(ctx)
        tool = tools.get("run_code")
        if tool is None:
            return tools
        schema = copy.deepcopy(tool.tool_def.parameters_json_schema)
        if self.focused_policy is not None and self.focused_policy.enabled:
            schema["properties"]["focused"] = {
                "type": "boolean",
                "default": False,
                "description": "Request a short read/check/command window. No inference, waiting or blind action retry occurs inside it.",
            }
            tool = replace(tool, args_validator=_FOCUSED_ARGS)
        tools["run_code"] = replace(
            tool,
            tool_def=replace(
                tool.tool_def,
                parameters_json_schema=schema,
                description=(tool.tool_def.description or "")
                + "\n\n"
                + local_helper_catalogue(self.include_context, self.include_selectors),
                metadata={**(tool.tool_def.metadata or {}), "local_result_helpers": LOCAL_HELPER_VERSION},
            ),
        )
        return tools

    async def _execute_code(self, code, ctx, tool, execution):
        state = self._run_state
        assert state is not None
        session = state.session
        # State belongs to the run and is shared by Harness's per-step copies.
        # A restart, timeout or worker replacement creates a different session.
        install = session is None or getattr(state, "_civ_helpers_session", None) is not session
        submitted = LOCAL_HELPER_SOURCE + "\n" + code if install else code
        if install:
            extra = ""
            if self.include_selectors:
                from civ_agent.runtime.code_mode_results import LOCAL_SELECTOR_SOURCE

                extra += "\n" + LOCAL_SELECTOR_SOURCE
            if self.include_context:
                from civ_agent.runtime.delivered_context import context_source

                extra += "\n" + context_source(ctx)
            submitted = LOCAL_HELPER_SOURCE + extra + "\n" + code
        try:
            return await super()._execute_code(submitted, ctx, tool, execution)
        except ModelRetry as error:
            if "construction_details" in code:
                raise ModelRetry(
                    str(error) + "\nconstruction_details: target=<a delivered city ID> is required; "
                    "horizonTurns also requires nonempty names. Bundle arguments use that same canonical schema."
                ) from error
            raise
        finally:
            if state.has_executed_feed and state.session is not None:
                vars(state)["_civ_helpers_session"] = state.session

    async def _complete(self, ctx, execution, result):
        returned = await super()._complete(ctx, execution, result)
        from civ_agent.runtime.tool_output import compact_tool_output

        returned.return_value = compact_tool_output(
            returned.return_value,
            (returned.metadata or {}).get("tool_returns", {}).values(),
            (returned.metadata or {}).get("tool_calls", {}),
        )
        returned.metadata = {**(returned.metadata or {}), "local_result_helpers": LOCAL_HELPER_VERSION}
        if self.include_selectors:
            from civ_agent.runtime.code_mode_results import LOCAL_SELECTOR_VERSION

            returned.metadata["local_result_selectors"] = LOCAL_SELECTOR_VERSION
        if self.include_context:
            from civ_agent.runtime.delivered_context import CONTEXT_VERSION
            from civ_agent.runtime.player_observations import PlayerObservations

            snapshot = next(
                (
                    cap.delivered_context(ctx)
                    for cap in ctx.capabilities.values()
                    if isinstance(cap, PlayerObservations)
                ),
                None,
            )
            returned.metadata["delivered_opening_context"] = {
                "version": CONTEXT_VERSION,
                "sha256": digest(snapshot) if snapshot is not None else None,
                "freshness": "original_opening_only",
            }
        return returned

    async def call_tool(self, name, tool_args, ctx, tool):
        if name != "run_code" or not tool_args.get("focused", False):
            return await super().call_tool(name, tool_args, ctx, tool)
        from civ_agent.runtime.execution_windows import focused_execution

        policy = self.focused_policy
        async with focused_execution(
            self.execution_windows,
            ctx.deps,
            run_id=ctx.run_id,
            parent_call_id=ctx.tool_call_id,
            code=tool_args["code"],
            enabled=bool(policy is not None and policy.enabled),
            seconds=policy.max_seconds if policy else 10.0,
            max_calls=policy.max_calls if policy else 6,
            queue_timeout=policy.queue_timeout if policy else 30.0,
        ) as window:
            result = await super().call_tool(name, tool_args, ctx, tool)
        if window is not None and isinstance(result, ToolReturn):
            result.metadata = {
                **(result.metadata or {}),
                "focused_window": {
                    "program_sha256": window.binding.program_sha256,
                    "operation_count": window.calls,
                    "acquired": window.deadline is not None,
                    "end_reason": window.ended,
                },
            }
        return result


@dataclass
class CheckedCodeMode(CodeMode[Any]):
    required: frozenset[str] = frozenset()
    execution_windows: Any = None
    focused_policy: Any = None
    include_context: bool = False
    include_selectors: bool = False

    def get_wrapper_toolset(self, toolset):
        reserved = RESULT_HELPERS
        if self.include_context:
            from civ_agent.runtime.delivered_context import CONTEXT_HELPERS

            reserved |= CONTEXT_HELPERS
        if self.include_selectors:
            from civ_agent.runtime.code_mode_results import RESULT_SELECTORS

            reserved |= RESULT_SELECTORS
        application = CheckedCatalogue(toolset, self.required, reserved=reserved)
        wrapped = super().get_wrapper_toolset(application)
        assert isinstance(wrapped, CodeModeToolset)
        return LocalCodeModeToolset(
            **{item.name: getattr(wrapped, item.name) for item in fields(CodeModeToolset) if item.init},
            execution_windows=self.execution_windows,
            focused_policy=self.focused_policy,
            include_context=self.include_context,
            include_selectors=self.include_selectors,
        )


def _public_id(call_id):
    return call_id if len(call_id) <= 96 else "sdk_" + digest(call_id)


def action_outcomes(call, value):
    if call.tool_name not in _ACTIONS:
        return []
    value = value.return_value if isinstance(value, ToolReturn) else value
    if not isinstance(value, dict):
        return [{"call": _public_id(call.tool_call_id), "status": "returned"}]
    outcomes = []
    for receipt in value.get("receipts", [])[:12]:
        result = receipt.get("result", {})
        outcome = {key: receipt[key] for key in ("operationId", "status", "tool") if key in receipt}
        if "error" in result:
            outcome["error"] = str(result["error"])[:120]
        outcomes.append(outcome)
    return [
        {
            "call": _public_id(call.tool_call_id),
            **{
                key: value[key]
                for key in ("decisionId", "error", "refreshRequired", "applied", "version", "id")
                if key in value
            },
            "receipts": outcomes,
            "unexecuted": value.get("unexecuted", [])[:12],
        }
    ]


@dataclass
class NativeFailureFence(AbstractCapability[Any]):
    """Python exception handling cannot suppress a native integrity/authority failure."""

    id: str | None = "native-code-fence"
    primary: BaseException | None = field(default=None, init=False)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock, init=False)
    outcomes: list = field(default_factory=list, init=False)
    rejected: list = field(default_factory=list, init=False)

    async def on_tool_validate_error(self, ctx, *, call, tool_def, args, error):
        if _parent.get() is None or not isinstance(error, ValidationError):
            raise error
        details = argument_feedback(error, tool_def.parameters_json_schema)
        if any(issue["fields"][-1:] == ["intent"] for issue in details["invalidFields"]):
            details["instruction"] += " Supply intent.reason or a delivered intent.cityPlan memory ID."
        if call.tool_name == "construction_details":
            details["instruction"] += (
                " construction_details requires target=<a delivered city ID>; horizonTurns requires nonempty names."
            )
        response = recovery_response(
            "INVALID_TOOL_ARGUMENTS",
            "Tool arguments were rejected before execution.",
            tool=call.tool_name,
            details=details,
        )
        if (pending := recoveries.get()) is not None:
            pending.append(response)
        # Native argument validation keeps its original SDK retry allowance.
        raise ModelRetry(json.dumps(response, ensure_ascii=False)) from None

    def correction(self, message, pending):
        response = next((item for item in reversed(pending) if json.dumps(item, ensure_ascii=False) in message), None)
        if response is None and "RESULT_FEEDBACK_REQUIRED" in message:
            response = next(
                (item for item in reversed(self.rejected) if item["recovery"].get("receipt") in message), None
            )
        if response is None:
            code = "CODE_RUNTIME_ERROR"
            if message.startswith("Syntax error"):
                code = "CODE_SYNTAX_ERROR"
            elif message.startswith("Type error"):
                code = "CODE_TYPE_ERROR"
            response = recovery_response(
                code,
                "The snippet stopped before completing.",
                details={
                    "instruction": "Correct the snippet using the diagnostic. Preserve confirmed effects; do not replay the whole snippet."
                },
            )
            response["diagnostic"] = message
        response = copy.deepcopy(response)
        response["action_outcomes"] = bounded_outcomes(self.outcomes)
        return json.dumps(response, ensure_ascii=False)

    async def for_run(self, ctx):
        return type(self)()

    def get_ordering(self):
        return CapabilityOrdering(position="outermost", wraps=[HandleDeferredToolCalls])

    def check(self):
        if self.primary is not None:
            raise self.primary

    async def wrap_tool_execute(self, ctx, *, call, tool_def, args, handler):
        self.check()
        if call.tool_name == "run_code":
            token = _parent.set(call.tool_call_id)
            fence_token = _active_fence.set(self)
            self.outcomes = []
            self.rejected = []
            pending = []
            recovery_token = recoveries.set(pending)
            try:
                try:
                    result = await handler(args)
                except BaseException as error:
                    self.check()
                    if isinstance(error, ToolRetryError):
                        feedback = replace(
                            error.tool_retry,
                            content=self.correction(str(error.tool_retry.content), pending),
                        )
                        raise ToolRetryError(feedback) from error
                    if isinstance(error, ModelRetry):
                        raise ModelRetry(self.correction(str(error), pending)) from error
                    raise
                self.check()
                if isinstance(result, ToolReturn) and isinstance(result.metadata, dict):
                    calls = result.metadata.get("tool_calls", {})
                    self.outcomes = [
                        outcome
                        for identifier, returned in result.metadata.get("tool_returns", {}).items()
                        if identifier in calls
                        for outcome in action_outcomes(calls[identifier], returned.content)
                    ]
                if self.outcomes:
                    # Runs after output-limit hooks; effects remain inline even
                    # when a script prints nothing or its output was spilled.
                    feedback = bounded_outcomes(self.outcomes)
                    if isinstance(result, ToolReturn):
                        result.return_value = {"output": result.return_value, "action_outcomes": feedback}
                    else:
                        result = {"output": result, "action_outcomes": feedback}
                return result
            finally:
                recoveries.reset(recovery_token)
                _parent.reset(token)
                _active_fence.reset(fence_token)
        if _parent.get() is None:
            return await handler(args)
        async with self.lock:
            self.check()
            try:
                result = await handler(args)
                value = result.return_value if isinstance(result, ToolReturn) else result
                code = value.get("error") if isinstance(value, dict) else None
                if isinstance(code, dict):
                    code = code.get("code")
                if isinstance(code, str) and code in _FATAL_RESULTS:
                    raise JournalConflict("CODE_MODE_NATIVE_AUTHORITY_RETIRED")
                self.outcomes.extend(action_outcomes(call, result))
                if isinstance(value, dict):
                    for receipt in value.get("receipts", []):
                        rejected = receipt.get("result", {})
                        error = rejected.get("error") if isinstance(rejected, dict) else None
                        if error:
                            code = error.get("code") if isinstance(error, dict) else error
                            if isinstance(code, str):
                                self.rejected.append(
                                    recovery_response(
                                        code,
                                        "The native call requires reconsideration.",
                                        tool=receipt.get("tool"),
                                        details={**rejected.get("recovery", {}), "receipt": receipt.get("operationId")},
                                    )
                                )
                return result
            except _CORRECTABLE:
                raise
            except BaseException as error:
                if self.primary is None:
                    self.primary = error
                raise


def bounded_outcomes(outcomes):
    if len(json.dumps(outcomes, ensure_ascii=False)) <= 8000:
        return outcomes
    # Every full child result remains in private SDK metadata and receipts.
    # Keep all rejected/unexecuted child references inline, with bounded details.
    unresolved = []
    for item in outcomes:
        if (
            item.get("unexecuted")
            or item.get("error")
            or any(receipt.get("status") in {"rejected", "unexecuted"} for receipt in item.get("receipts", []))
        ):
            unresolved.append(item["call"])
    return {
        "total_calls": len(outcomes),
        "unresolved_calls": unresolved,
        "recent_calls": [item["call"] for item in outcomes[-8:]],
        "details": "Full child outcomes and native receipts are retained in this run's history.",
    }


def code_mode_capabilities(
    policy, *, required, store, execution_windows=None, allow_focused=True, allow_delivered_context=True
):
    capabilities: list[AbstractCapability[Any]] = [
        NativeFailureFence(),
        CheckedCodeMode(
            required=frozenset(required),
            dynamic_catalog=False,
            max_tool_calls=policy.code_mode.max_tool_calls,
            execution_windows=execution_windows,
            focused_policy=getattr(policy.code_mode, "focused_windows", None) if allow_focused else None,
            include_context=policy.code_mode.delivered_context and allow_delivered_context,
            include_selectors=policy.code_mode.typed_selectors,
            resource_limits={
                "max_duration_secs": policy.code_mode.execution_timeout,
                "max_memory": policy.code_mode.memory_bytes,
                "max_suspensions": policy.code_mode.max_suspensions,
            },
        ),
    ]
    if policy.output.enabled:
        capabilities.append(
            AvailabilityOutputLimits(
                bands=[
                    Band(
                        over=policy.output.max_characters,
                        action=Spill(
                            preview_chars=policy.output.preview_characters,
                            then=Passthrough(),
                        ),
                    )
                ],
                tool_filter=["run_code"],
                store=store,
            )
        )
    return capabilities


class AvailabilityOutputLimits(ToolOutputLimits[Any]):
    async def after_tool_execute(self, ctx, *, call, tool_def, args, result):
        limited = await super().after_tool_execute(ctx, call=call, tool_def=tool_def, args=args, result=result)
        notice = "Overflow storage unavailable; committed effects and native receipts remain valid.\n"
        if (
            isinstance(limited, ToolReturn)
            and isinstance(limited.return_value, str)
            and isinstance(result, ToolReturn)
            and limited.return_value != result.return_value
            and not (isinstance(limited.metadata, dict) and "overflow_handle" in limited.metadata)
        ):
            limited.return_value = notice + limited.return_value
        if isinstance(limited, str) and limited is not result:
            return notice + limited
        return limited


def required_functions(catalogue):
    """Every permitted catalogue entry must have a generated function or action union."""
    required = set()
    for entry in catalogue:
        name = entry["name"]
        if name == "finish_role":
            continue
        if name == "player_command":
            required.add("execute_commands")
        elif name in READS or name in {"economy_wait", "inspect_bundle", "command_help"}:
            required.add(name)
        else:
            required.add("act")
    return required
