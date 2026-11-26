"""Fresh Pydantic AI role runs over the existing authenticated domain services."""

import copy
import json
import logging
import warnings
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any
from uuid import uuid4

from jsonschema import ValidationError as JsonSchemaError
from pydantic import Field, create_model
from pydantic_ai import Agent, DeferredToolRequests, FunctionToolset, ModelRetry, RunContext, Tool, ToolOutput
from pydantic_ai.capabilities import AbstractCapability, HandleDeferredToolCalls, PrepareTools
from pydantic_ai.concurrency import AbstractConcurrencyLimiter
from pydantic_ai.messages import ModelMessage
from pydantic_ai.models import Model
from pydantic_ai.models.concurrency import ConcurrencyLimitedModel
from pydantic_ai.tools import DeferredToolResults
from pydantic_ai.toolsets import DynamicToolset
from pydantic_ai.usage import RunUsage, UsageLimits
from pydantic_ai_harness.step_persistence import StepStore
from pydantic_ai_harness.warn_on_cache_busts import CacheBustWarning, CacheNotEnabledWarning, WarnOnCacheBusts

from civ_agent.construction_intents import construction_tool
from civ_agent.decision_contracts import NATIVE_MODELS, compile_decision_contract
from civ_agent.execution_policy import legacy_execution_policy
from civ_agent.game_commands import command_kinds
from civ_agent.orchestration.authored_memory import authored_memory_capability
from civ_agent.orchestration.authored_plan_tools import authored_plan_capability
from civ_agent.orchestration.contracts import GatewayReceipt, ReviewRequest, RoleReport, RoleRequest
from civ_agent.orchestration.gateway_execution import native_call_id
from civ_agent.orchestration.gateway_tools import command_type, describe_native_result, gateway_tools, parameterize
from civ_agent.orchestration.investment_workflow import investment_workflow_capability, workflow_instructions
from civ_agent.orchestration.player_plans import player_planning, sequential_plan_tools
from civ_agent.orchestration.store import JournalConflict
from civ_agent.orchestration.transcripts import record_nested_result
from civ_agent.protocol import JsonObject
from civ_agent.runtime.capabilities import ContextUsageCallback, agent_capabilities, run_metadata
from civ_agent.runtime.code_mode import (
    _parent,
    code_mode_capabilities,
    detached_code_mode_context,
    required_functions,
)
from civ_agent.runtime.code_mode_instructions import with_code_mode_instructions
from civ_agent.runtime.model_policy import (
    AttemptCallback,
    ModelPolicySettings,
    StatusCallback,
)
from civ_agent.runtime.outcomes import EffectClaim, VerifiedOutcomes
from civ_agent.runtime.overflow import ScopedOverflowStore
from civ_agent.runtime.player_observations import PlayerObservations
from civ_agent.runtime.preload import build_preload, model_cache_settings

type Invoke = Callable[[RoleRequest, str, int, JsonObject], Awaitable[GatewayReceipt]]
type ExecuteCommands = Callable[[RoleRequest, str, str, int, list[JsonObject]], Awaitable[JsonObject]]
type WaitCallback = Callable[[RoleRequest, DeferredToolRequests], Awaitable[DeferredToolResults]]


@dataclass(frozen=True)
class LocalAgentServices:
    invoke: Invoke
    step_store: StepStore
    gateway: Any = None
    execute_commands: ExecuteCommands | None = None
    on_observation_delivered: Callable[[RoleRequest, str, int], Awaitable[None]] | None = None
    on_startup_delivered: Callable[[RoleRequest, str, bool], Awaitable[None]] | None = None
    overflow_root: Path | None = None
    cache_account_scope: str | None = None
    cache_endpoint: str | None = None
    on_wait: WaitCallback | None = None
    on_model_status: StatusCallback | None = None
    on_model_attempt: AttemptCallback | None = None
    remaining_model_attempts: Callable[[Any], int] | None = None
    on_context_usage: ContextUsageCallback | None = None
    model_limiter: AbstractConcurrencyLimiter | None = None
    run_metadata: Mapping[str, str] | None = None
    model_policy: ModelPolicySettings = ModelPolicySettings()
    admission_factory: Callable[[str], AbstractCapability[Any]] | None = None
    execution_windows: Any = None


@dataclass(frozen=True)
class CompletedRole:
    report: RoleReport
    completion: JsonObject


@dataclass(frozen=True)
class LocalRoleResult:
    report: RoleReport
    completion: JsonObject
    usage: RunUsage
    messages: list[ModelMessage]
    run_id: str


@dataclass(frozen=True)
class LocalReportResult:
    report: Any
    usage: RunUsage
    messages: list[ModelMessage]
    run_id: str


def typed_report_bundle(tool, arguments_model):
    """Retain the frozen read implementation while using typed query validation."""
    original = tool.function_schema.function

    async def read(ctx, arguments):
        return await original(ctx, arguments)

    read.__annotations__ = {"ctx": RunContext[Any], "arguments": arguments_model, "return": dict[str, Any]}
    schema = Tool(read, takes_ctx=True).function_schema
    return replace(schema, return_schema=tool.function_schema.return_schema)


def _confirmed(result: JsonObject) -> bool:
    if result.get("roleFinished") is True:
        return True
    return any(
        item.get("result", {}).get("roleFinished") is True
        for item in result.get("receipts", [])
        if isinstance(item, dict)
    )


def _completion_model(request: RoleRequest, catalogue: list[JsonObject], *, contract=None):
    contract = contract or compile_decision_contract(request.purpose, catalogue)
    base = contract.models["finish_role"]
    fields: dict[str, Any] = {}
    if request.gameplay_policy.version:
        fields["effectClaims"] = (list[EffectClaim], Field(default_factory=list, max_length=48))
    return create_model(
        "Local" + request.purpose.title() + "Completion",
        __base__=base,
        summary=(str, Field(min_length=1, max_length=8000, description="Short factual report of this role's work.")),
        **fields,
    )


class LocalAgentRunner:
    def __init__(self, services: LocalAgentServices):
        self.services = services
        self.cache_warnings = WarnOnCacheBusts()

    def _model(self, model: Model) -> Model:
        return (
            ConcurrencyLimitedModel(model, self.services.model_limiter)
            if self.services.model_limiter is not None
            else model
        )

    def _capabilities(self, deps, *, name, role_capabilities=()):
        services = self.services
        contract = getattr(deps, "provider_contract", None)
        return agent_capabilities(
            store=services.step_store,
            metadata=run_metadata(deps, agent_name=name, extra=services.run_metadata),
            settings=services.model_policy,
            cache_warnings=self.cache_warnings,
            context_window=contract.context_window if contract else None,
            on_status=services.on_model_status,
            on_attempt=services.on_model_attempt,
            remaining_model_attempts=services.remaining_model_attempts,
            on_context_usage=services.on_context_usage,
            role_capabilities=role_capabilities,
            deps=deps,
            cache_account_scope=services.cache_account_scope,
            cache_endpoint=services.cache_endpoint,
            admission_factory=services.admission_factory,
        )

    def make_agent(
        self, request: RoleRequest, *, catalogue: list[JsonObject], model: Model, startup: str | None = None
    ):
        services = self.services
        frozen_catalogue = copy.deepcopy(catalogue)
        completion_contract = compile_decision_contract(
            request.purpose, frozen_catalogue, policy_key=request.gameplay_policy.model_dump(mode="json")
        )
        completion_model = _completion_model(request, frozen_catalogue, contract=completion_contract)

        async def invoke(deps, call_id, model_step, decision):
            bounded = native_call_id(call_id)
            result = await services.invoke(deps, bounded, model_step, decision)
            if _parent.get() is not None and decision["kind"] == "inspect":
                if services.on_observation_delivered is not None:
                    await services.on_observation_delivered(deps, bounded, model_step)
            return result

        async def tools(ctx):
            if services.gateway is not None and hasattr(services.gateway, "catalogue"):
                granted = await services.gateway.catalogue(ctx.deps)
                if granted.catalogue != frozen_catalogue:
                    raise JournalConflict("GATEWAY_CATALOGUE_CHANGED")
            ordinary = [entry for entry in frozen_catalogue if entry["name"] not in {"finish_role", "player_command"}]
            prepared = gateway_tools(
                ctx.deps.purpose,
                ordinary,
                invoke,
                include_controls=bool(request.execution_policy.version),
                command_catalogue=next(
                    (entry for entry in frozen_catalogue if entry["name"] == "player_command"), None
                ),
                include_bundle_receipts=request.execution_policy.code_mode.typed_selectors,
            )
            for tool in prepared:
                tool.max_retries = 2
            return FunctionToolset(prepared, max_retries=2)

        async def complete(ctx, completion):
            if not ctx.tool_call_id:
                raise JournalConflict("LOCAL_COMPLETION_CALL_REQUIRED")
            fields = completion.model_dump(mode="json", by_alias=True, exclude_unset=True)
            report = RoleReport(summary=fields.pop("summary"))
            claims = fields.pop("effectClaims", [])
            for capability in ctx.capabilities.values():
                if isinstance(capability, VerifiedOutcomes):
                    capability.validate(claims)
            try:
                # Check the canonical boundary before claiming a gateway call.
                # A schema-only mismatch has no native effect to reconcile.
                completion_contract.encode({"kind": "finish", "arguments": fields})
            except (ValueError, JsonSchemaError):
                raise ModelRetry(
                    "Completion arguments do not match the native wire contract. Correct their required fields and values; use reviewTurn:null for an unscheduled request. Do not repeat committed actions."
                ) from None
            result = await services.invoke(
                ctx.deps,
                native_call_id(ctx.tool_call_id),
                ctx.deps.model_step_offset + ctx.run_step,
                {"kind": "finish", "arguments": fields},
            )
            if not _confirmed(result.result):
                # This is a definite public completion rejection. Transport,
                # integrity and uncertain-effect exceptions are not caught.
                feedback = json.dumps(result.result, ensure_ascii=False, separators=(",", ":"))[:8000]
                raise ModelRetry("The role is not complete. Resolve this feedback before finishing: " + feedback)
            return CompletedRole(report=report, completion=result.result)

        complete.__annotations__ = {
            "ctx": RunContext[RoleRequest],
            "completion": completion_model,
            "return": CompletedRole,
        }

        async def deferred(ctx, requests):
            try:
                from civ_agent.runtime.execution_windows import detached_focus

                if services.on_wait is None:
                    raise RuntimeError("LOCAL_ROLE_WAIT_HANDLER_REQUIRED")
                with detached_focus("deferred_wait"), detached_code_mode_context():
                    result = await services.on_wait(ctx.deps, requests)
                if set(result.calls) != {call.tool_call_id for call in requests.calls}:
                    raise JournalConflict("LOCAL_DEFERRED_SETTLEMENT_CONFLICT")
                for call in requests.calls:
                    effect = await services.step_store.get_tool_effect(
                        run_id=ctx.run_id, tool_call_id=call.tool_call_id
                    )
                    if effect is None or effect.tool_name != call.tool_name or effect.status != "started":
                        raise JournalConflict("LOCAL_DEFERRED_EFFECT_CONFLICT")
                    await services.step_store.record_tool_effect(
                        replace(effect, status="completed", ended_at=datetime.now(UTC))
                    )
                    await record_nested_result(ctx, call, result.calls[call.tool_call_id])
                return result
            except BaseException as error:
                from civ_agent.runtime.code_mode import NativeFailureFence

                for capability in ctx.capabilities.values():
                    if isinstance(capability, NativeFailureFence) and capability.primary is None:
                        capability.primary = error
                raise

        role_capabilities: list[AbstractCapability[Any]] = [
            HandleDeferredToolCalls(deferred),
            PlayerObservations(
                invoke=services.invoke,
                on_delivered=services.on_observation_delivered,
                startup=startup,
                on_startup_delivered=services.on_startup_delivered,
                export_context=request.execution_policy.code_mode.delivered_context,
            ),
        ]
        if request.gameplay_policy.version:
            from pydantic_ai_harness import DetectLoops, SystemReminders
            from pydantic_ai_harness.system_reminders import Reminder

            from civ_agent.gameplay_policy import OPPORTUNITY_GUIDANCE

            role_capabilities.extend(
                [
                    VerifiedOutcomes(),
                    DetectLoops(action="nudge"),
                    SystemReminders(reminders=[Reminder(OPPORTUNITY_GUIDANCE, interval=5, tag=None)]),
                ]
            )
        if services.gateway is not None:
            role_capabilities.extend(
                [
                    authored_memory_capability(services.gateway),
                    authored_plan_capability(services.gateway, enabled=lambda _ctx: True),
                    investment_workflow_capability(services.gateway),
                    player_planning(lambda: services.gateway),
                    PrepareTools(sequential_plan_tools),
                ]
            )

        command_entry = next((entry for entry in frozen_catalogue if entry["name"] == "player_command"), None)
        toolsets: list[Any] = [DynamicToolset(tools, per_run_step=False, id="local-gateway")]
        if command_entry is not None:
            command = command_type(
                command_entry["inputSchema"], include_metadata=True, review_after=request.purpose == "military"
            )

            async def execute(ctx, commands):
                checked = []
                for item in commands:
                    body = item.model_dump(mode="json", by_alias=True, exclude_unset=True)
                    metadata = {key: body.pop(key) for key in ("intent", "reviewAfter") if key in body}
                    checked.append({"tool": "player_command", "arguments": {"command": body, **metadata}})
                step = ctx.deps.model_step_offset + ctx.run_step
                if services.execute_commands is not None:
                    return await services.execute_commands(
                        ctx.deps, ctx.run_id, native_call_id(ctx.tool_call_id), step, checked
                    )
                return (
                    await services.invoke(
                        ctx.deps, native_call_id(ctx.tool_call_id), step, {"kind": "act", "operations": checked}
                    )
                ).result

            execute.__annotations__ = {
                "ctx": RunContext[RoleRequest],
                "commands": parameterize(Annotated, (parameterize(list, command), Field(min_length=1, max_length=12))),
                "return": dict[str, Any],
            }
            toolsets.append(
                FunctionToolset(
                    [
                        Tool(
                            execute,
                            name="execute_commands",
                            takes_ctx=True,
                            max_retries=2,
                            sequential=True,
                            prepare=describe_native_result,
                        )
                    ]
                )
            )

        if command_entry is not None and "QUEUE_CONSTRUCTION" in command_kinds(command_entry["inputSchema"]):

            async def construct(ctx, command):
                operations = [{"tool": "player_command", "arguments": {"command": command}}]
                step = ctx.deps.model_step_offset + ctx.run_step
                if services.execute_commands is not None:
                    return await services.execute_commands(
                        ctx.deps, ctx.run_id, native_call_id(ctx.tool_call_id), step, operations
                    )
                return (
                    await services.invoke(
                        ctx.deps, native_call_id(ctx.tool_call_id), step, {"kind": "act", "operations": operations}
                    )
                ).result

            toolsets.append(FunctionToolset([construction_tool(construct)]))

        if request.execution_policy.code_mode.enabled:
            if services.overflow_root is None and request.execution_policy.output.enabled:
                raise ValueError("CODE_MODE_OVERFLOW_ROOT_REQUIRED")
            role_capabilities.extend(
                code_mode_capabilities(
                    request.execution_policy,
                    required=required_functions(frozen_catalogue),
                    store=ScopedOverflowStore.for_owner(services.overflow_root, request)
                    if services.overflow_root is not None
                    else None,
                    execution_windows=services.execution_windows,
                    allow_focused=request.purpose != "communication",
                )
            )
        capabilities = self._capabilities(request, name="player_role_v1", role_capabilities=role_capabilities)

        return Agent(
            self._model(model),
            name="player_role_v1",
            deps_type=RoleRequest,
            output_type=ToolOutput(complete, name="finish_role"),
            retries={"tools": 2, "output": 4},
            toolsets=toolsets,
            capabilities=capabilities,
        )

    async def run_role(
        self,
        request: RoleRequest,
        *,
        startup: dict,
        catalogue: list[JsonObject],
        model: Model,
        usage: RunUsage | None = None,
    ) -> LocalRoleResult:
        agent = self.make_agent(request, catalogue=catalogue, model=model, startup=startup["user"])
        run_id = str(uuid4())
        preload = build_preload(
            request.purpose,
            instructions=with_code_mode_instructions(
                workflow_instructions(
                    startup["instructions"], enabled=request.execution_policy.follow_through_workflow
                ),
                enabled=request.execution_policy.code_mode.enabled,
                focused=bool(
                    getattr(getattr(request.execution_policy.code_mode, "focused_windows", None), "enabled", False)
                ),
                procedures=request.execution_policy.code_mode.procedure_examples,
            ),
        )
        with agent.parallel_tool_call_execution_mode("sequential"):
            result = await agent.run(
                startup["user"],
                instructions=preload.instructions,
                deps=request,
                run_id=run_id,
                conversation_id=request.scope.identity("conversation", "role"),
                usage=usage,
                # Retained request_limit fields are archive metadata. A role
                # may keep working across model calls and fresh conversations.
                usage_limits=UsageLimits(request_limit=None),
                model_settings=model_cache_settings(request.selection, preload),
                metadata=run_metadata(request, agent_name="player_role_v1", extra=self.services.run_metadata),
            )
        return LocalRoleResult(
            report=result.output.report,
            completion=result.output.completion,
            usage=result.usage,
            messages=result.all_messages(),
            run_id=run_id,
        )

    async def run_report(
        self,
        deps: Any,
        *,
        startup: dict,
        model: Model,
        output_type: Any,
        name: str,
        tools: list[Tool[Any]] | None = None,
        validate: Callable[[RunContext[Any], Any], Awaitable[Any]] | None = None,
        usage: RunUsage | None = None,
    ) -> LocalReportResult:
        """Run a typed strategic/peace report with the same provider policy."""
        services = self.services
        selection = deps.selection
        execution_policy = getattr(deps, "execution_policy", legacy_execution_policy())
        report_tools = [copy.copy(tool) for tool in tools or ()]
        for tool in report_tools:
            tool.sequential = True
            if tool.function_schema.return_schema is not None:
                tool.function_schema = replace(
                    tool.function_schema,
                    return_schema={
                        "type": "object",
                        "title": "FrozenReadResult",
                        "additionalProperties": True,
                        "properties": {
                            "source": {"type": "string"},
                            "data": {
                                "anyOf": [
                                    {"type": "object", "additionalProperties": True},
                                    {"type": "array", "items": {}},
                                ]
                            },
                        },
                        "required": ["source", "data"],
                    },
                )
        if execution_policy.code_mode.enabled:
            from typing import get_args

            from civ_agent.inspection_bundle import InspectionQuery, typed_bundle_model
            from civ_agent.strategic_review import REVIEW_READ_TOOLS
            from civ_agent.tool_schemas import tool_model

            bundle_reads = tuple(
                (name, NATIVE_MODELS.get(name) or tool_model(name, "review"))
                for name in sorted(get_args(InspectionQuery.model_fields["tool"].annotation))
                if name in REVIEW_READ_TOOLS
            )
            for tool in report_tools:
                if tool.name == "inspect_bundle" and bundle_reads:
                    arguments = typed_bundle_model(bundle_reads)
                    tool.function_schema = typed_report_bundle(tool, arguments)
        report_capabilities = []
        if execution_policy.code_mode.enabled:
            if services.overflow_root is None and execution_policy.output.enabled:
                raise ValueError("CODE_MODE_OVERFLOW_ROOT_REQUIRED")
            report_capabilities.extend(
                code_mode_capabilities(
                    execution_policy,
                    required={tool.name for tool in report_tools},
                    store=ScopedOverflowStore.for_owner(services.overflow_root, deps)
                    if services.overflow_root is not None
                    else None,
                    allow_focused=False,
                    allow_delivered_context=False,
                )
            )
        agent = Agent(
            self._model(model),
            name=name,
            deps_type=type(deps),
            output_type=ToolOutput(output_type, name="submit_report"),
            tools=report_tools,
            retries={"tools": 2, "output": deps.attempt_limit - 1 if isinstance(deps, ReviewRequest) else 4},
            capabilities=self._capabilities(deps, name=name, role_capabilities=report_capabilities),
        )
        if validate is not None:

            @agent.output_validator
            async def validated(ctx: RunContext[Any], report: Any):
                accepted = await validate(ctx, report)
                return report if accepted is None else accepted

        run_id = str(uuid4())
        scope = deps.request.scope if hasattr(deps, "request") else deps.scope
        preload = build_preload(
            name,
            instructions=with_code_mode_instructions(
                startup["instructions"], enabled=execution_policy.code_mode.enabled, report=True
            ),
        )
        with agent.parallel_tool_call_execution_mode("sequential"):
            result = await agent.run(
                startup["user"],
                instructions=preload.instructions,
                deps=deps,
                run_id=run_id,
                conversation_id=scope.identity("conversation", name),
                usage=usage,
                usage_limits=UsageLimits(request_limit=None),
                model_settings=model_cache_settings(selection, preload),
                metadata=run_metadata(deps, agent_name=name, extra=services.run_metadata),
            )
        return LocalReportResult(
            report=result.output, usage=result.usage, messages=result.all_messages(), run_id=run_id
        )


def configure_cache_warning_logging():
    """Route maintained cache warnings to Python WARNING logs and tracing log handlers."""
    # captureWarnings installs a NullHandler on py.warnings. A command-line
    # controller also needs a real handler, otherwise warnings can disappear.
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
    logging.captureWarnings(True)
    warnings.simplefilter("always", CacheBustWarning)
    warnings.simplefilter("always", CacheNotEnabledWarning)
    global _warning_fallback
    if warnings.showwarning is not _safe_cache_warning:
        _warning_fallback = warnings.showwarning
        warnings.showwarning = _safe_cache_warning


_warning_fallback = warnings.showwarning


def _safe_cache_warning(message, category, filename, lineno, file=None, line=None):
    """Only numeric cache evidence and a fixed reason reach logs; never arbitrary warning text."""
    logger = logging.getLogger("py.warnings")
    if isinstance(message, CacheBustWarning):
        reason = message.reason if message.reason in {"unexpected", "unknown"} else "unknown"
        logger.warning(
            "Cache hit collapsed: reason=%s established_tokens=%d cache_read_tokens=%d missed_tokens=%d",
            reason,
            message.established_tokens,
            message.cache_read_tokens,
            message.missed_tokens,
        )
    elif isinstance(message, CacheNotEnabledWarning):
        logger.warning("Prompt caching was not enabled for this request")
    else:
        _warning_fallback(message, category, filename, lineno, file, line)
