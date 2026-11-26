"""Typed strategy and city intent; generic Harness Memory stays plain notes."""

from collections.abc import Callable
from typing import Any, Literal

from pydantic import ValidationError
from pydantic.json_schema import SkipJsonSchema
from pydantic_ai import FunctionToolset, ModelRetry, RunContext, Tool, ToolFailed
from pydantic_ai.capabilities import AbstractCapability, durable_operation
from pydantic_ai.capabilities.abstract import RawToolArgs, ValidatedToolArgs, WrapToolExecuteHandler
from pydantic_ai.messages import ToolCallPart
from pydantic_ai.tools import ToolDefinition

from civ_agent.memory_records import CityPlan, Source, Strategy
from civ_agent.orchestration.authored_memory import AuthoredMemoryReply, AuthoredMemoryRequest
from civ_agent.orchestration.authored_plan_service import (
    AuthoredDocumentReply,
    DeleteAuthoredDocument,
    DocumentOperation,
    ReadAuthoredDocument,
    SaveAuthoredDocument,
)
from civ_agent.orchestration.contracts import GatewayDependencies, GatewayJob, digest
from civ_agent.orchestration.store import JournalConflict
from civ_agent.protocol import JsonObject
from civ_agent.schemas import argument_feedback
from civ_agent.tool_recovery import RecoverableToolError, recovery_response

PLAN_READ_TOOLS = frozenset({"read_city_plan", "read_strategy"})
CITY_MUTATION_TOOLS = frozenset({"save_city_plan", "delete_city_plan"})
STRATEGY_MUTATION_TOOLS = frozenset({"save_strategy", "delete_strategy"})
AUTHORED_PLAN_TOOLS = PLAN_READ_TOOLS | CITY_MUTATION_TOOLS | STRATEGY_MUTATION_TOOLS
MEMORY_TOOLS = frozenset({"read_memory", "write_memory", "search_memory", "delete_memory"})
_OPTIONAL_TOOLS = AUTHORED_PLAN_TOOLS | MEMORY_TOOLS


class CityPlanIntent(CityPlan):
    type: SkipJsonSchema[Literal["city_plan"]] = "city_plan"


class StrategyIntent(Strategy):
    type: SkipJsonSchema[Literal["strategy"]] = "strategy"


def typed_plans_enabled(ctx: RunContext[GatewayDependencies]) -> bool:
    return bool(getattr(ctx.deps, "typed_authored_plans", False))


class AuthoredPlanTools(AbstractCapability[GatewayDependencies]):
    id = "authored-plans-v1"

    def __init__(self, client, *, enabled: Callable[[RunContext[GatewayDependencies]], bool] = typed_plans_enabled):
        self.client, self.enabled = client, enabled
        self._toolset: FunctionToolset[GatewayDependencies] | None = None

    async def prepare_tools(
        self, ctx: RunContext[GatewayDependencies], tool_defs: list[ToolDefinition]
    ) -> list[ToolDefinition]:
        if not self.enabled(ctx):
            return [tool for tool in tool_defs if tool.name not in AUTHORED_PLAN_TOOLS]
        allowed = PLAN_READ_TOOLS
        if ctx.deps.purpose in {"gameplay", "economy"}:
            allowed |= CITY_MUTATION_TOOLS
        if ctx.deps.purpose == "gameplay":
            allowed |= STRATEGY_MUTATION_TOOLS
        return [
            tool
            for tool in tool_defs
            if (tool.name not in AUTHORED_PLAN_TOOLS or tool.name in allowed)
            and not (ctx.deps.purpose == "economy" and tool.name in {"write_memory", "delete_memory"})
        ]

    async def on_tool_validate_error(
        self,
        ctx: RunContext[GatewayDependencies],
        *,
        call: ToolCallPart,
        tool_def: ToolDefinition,
        args: RawToolArgs,
        error: ValidationError | ModelRetry,
    ) -> ValidatedToolArgs:
        if not self.enabled(ctx) or tool_def.name not in _OPTIONAL_TOOLS:
            raise error
        details = (
            argument_feedback(error, tool_def.parameters_json_schema)
            if isinstance(error, ValidationError)
            else {"instruction": error.message}
        )
        raise RecoverableToolError(
            recovery_response(
                "INVALID_TOOL_ARGUMENTS",
                "The optional authored edit was not applied.",
                tool=call.tool_name,
                details=details,
            )
        ) from None

    async def wrap_tool_execute(
        self,
        ctx: RunContext[GatewayDependencies],
        *,
        call: ToolCallPart,
        tool_def: ToolDefinition,
        args: ValidatedToolArgs,
        handler: WrapToolExecuteHandler,
    ) -> Any:
        # Memory returns its definitive author rejection through its own native
        # durable operation before re-raising ModelRetry at the tool boundary.
        # Integrity, authentication, epoch, transport and arbitrary failures are
        # not caught here and retain their normal diagnostic behavior.
        try:
            return await handler(args)
        except ModelRetry as error:
            if not self.enabled(ctx) or tool_def.name not in MEMORY_TOOLS:
                raise
            raise ToolFailed("The optional memory call was not applied. " + error.message) from None

    @staticmethod
    def _identity(ctx: RunContext[GatewayDependencies]) -> GatewayJob:
        return GatewayJob(scope=ctx.deps.scope, job=ctx.deps.job, purpose=ctx.deps.purpose)

    @staticmethod
    def _step(ctx: RunContext[GatewayDependencies]) -> int:
        return ctx.deps.model_step_offset + ctx.run_step

    @staticmethod
    def _program_delivery(ctx: RunContext[GatewayDependencies]) -> bool:
        policy = getattr(ctx.deps, "execution_policy", None)
        if policy is None or not policy.code_mode.enabled:
            return False
        from civ_agent.runtime.code_mode import _parent

        return _parent.get() is not None

    def _operation(self, ctx: RunContext[GatewayDependencies], semantic: JsonObject) -> DocumentOperation:
        if not ctx.run_id or not ctx.tool_call_id:
            raise JournalConflict("AUTHORED_TOOL_CONTEXT_CONFLICT")
        identity = self._identity(ctx).model_dump(mode="json")
        return DocumentOperation(
            id=digest([identity, ctx.run_id, ctx.run_step, ctx.tool_call_id]), fingerprint=digest([identity, semantic])
        )

    async def _request(self, ctx: RunContext[GatewayDependencies], call) -> AuthoredMemoryReply:
        request = AuthoredMemoryRequest(identity=self._identity(ctx), call=call)
        client = self.client if hasattr(self.client, "post") else self.client()
        return AuthoredMemoryReply.model_validate(await client.post("memory", request))

    @staticmethod
    def _known_rejection(reply: AuthoredMemoryReply) -> AuthoredDocumentReply | None:
        if reply.error == "operation_conflict":
            raise JournalConflict("AUTHORED_OPERATION_CONFLICT")
        if reply.error == "version_conflict":
            return AuthoredDocumentReply(
                error="version_conflict", feedback="The plan changed after it was read. Read it again before saving."
            )
        if reply.error in {"invalid_document", "policy_rejected"}:
            return AuthoredDocumentReply(
                error="policy_rejected", feedback=reply.feedback or "This authored edit was rejected and not applied."
            )
        return None

    @durable_operation("read")
    async def read(self, ctx: RunContext[GatewayDependencies], call: ReadAuthoredDocument) -> AuthoredDocumentReply:
        reply = await self._request(ctx, call)
        return self._known_rejection(reply) or AuthoredDocumentReply.model_validate(reply.result)

    @durable_operation("mutate")
    async def apply_document(
        self, ctx: RunContext[GatewayDependencies], call: SaveAuthoredDocument | DeleteAuthoredDocument
    ) -> AuthoredDocumentReply:
        reply = await self._request(ctx, call)
        return self._known_rejection(reply) or AuthoredDocumentReply.model_validate(reply.result)

    @staticmethod
    def _result(reply: AuthoredDocumentReply) -> JsonObject:
        if reply.error is not None:
            reads = []
            if reply.id and reply.id.startswith("city-"):
                reads = [{"tool": "read_city_plan", "arguments": {"city": reply.id[5:-3]}}]
            elif reply.id == "strategy.md":
                reads = [{"tool": "review_investments", "arguments": {}}]
            raise RecoverableToolError(
                recovery_response(
                    reply.code or "AUTHORED_" + reply.error.upper(),
                    reply.feedback or "The authored edit was rejected; the existing plan is unchanged.",
                    details={
                        "instruction": reply.feedback or "Review the current plan before editing.",
                        "reads": reads,
                        **(reply.recovery or {}),
                    },
                )
            )
        return reply.model_dump(mode="json", exclude_none=True)

    async def mutate(self, ctx, call) -> JsonObject:
        return self._result(await self.apply_document(ctx, call))

    def get_toolset(self):
        if self._toolset is not None:
            return self._toolset

        async def read_city_plan(ctx: RunContext[GatewayDependencies], city: Source) -> JsonObject:
            """Read one owned city's current intent before changing it. Code keeps its version for safe updates."""
            return self._result(
                await self.read(
                    ctx,
                    ReadAuthoredDocument(
                        document="city_plan",
                        city=city,
                        model_step=self._step(ctx),
                        program_delivered=self._program_delivery(ctx),
                    ),
                )
            )

        async def save_city_plan(ctx: RunContext[GatewayDependencies], plan: CityPlanIntent) -> JsonObject:
            """Save the complete intended plan for an owned city after reading it. No filename, JSON text or old_text is needed."""
            semantic = {"action": "save_document", "content": plan.model_dump(mode="json")}
            return await self.mutate(
                ctx,
                SaveAuthoredDocument(
                    content=plan, operation=self._operation(ctx, semantic), model_step=self._step(ctx)
                ),
            )

        async def delete_city_plan(ctx: RunContext[GatewayDependencies], city: Source) -> JsonObject:
            """Delete one owned city's plan after reading it; code preserves version checks and replay receipts."""
            semantic = {"action": "delete_document", "document": "city_plan", "city": city}
            return await self.mutate(
                ctx,
                DeleteAuthoredDocument(
                    document="city_plan",
                    city=city,
                    operation=self._operation(ctx, semantic),
                    model_step=self._step(ctx),
                ),
            )

        async def read_strategy(ctx: RunContext[GatewayDependencies]) -> JsonObject:
            """Read the player's current strategic intent before changing it. Authored claims are not game facts."""
            return self._result(
                await self.read(
                    ctx,
                    ReadAuthoredDocument(
                        document="strategy", model_step=self._step(ctx), program_delivered=self._program_delivery(ctx)
                    ),
                )
            )

        async def save_strategy(ctx: RunContext[GatewayDependencies], strategy: StrategyIntent) -> JsonObject:
            """Replace the complete strategy after reading it. Code derives its storage path, JSON and version check."""
            semantic = {"action": "save_document", "content": strategy.model_dump(mode="json")}
            return await self.mutate(
                ctx,
                SaveAuthoredDocument(
                    content=strategy, operation=self._operation(ctx, semantic), model_step=self._step(ctx)
                ),
            )

        async def delete_strategy(ctx: RunContext[GatewayDependencies]) -> JsonObject:
            """Delete the current strategy after reading it; this does not cancel or change native gameplay actions."""
            semantic = {"action": "delete_document", "document": "strategy", "city": None}
            return await self.mutate(
                ctx,
                DeleteAuthoredDocument(
                    document="strategy", operation=self._operation(ctx, semantic), model_step=self._step(ctx)
                ),
            )

        self._toolset = FunctionToolset(
            [
                Tool(read_city_plan, takes_ctx=True, max_retries=0),
                Tool(save_city_plan, takes_ctx=True, max_retries=0),
                Tool(delete_city_plan, takes_ctx=True, max_retries=0),
                Tool(read_strategy, takes_ctx=True, max_retries=0),
                Tool(save_strategy, takes_ctx=True, max_retries=0),
                Tool(delete_strategy, takes_ctx=True, max_retries=0),
            ],
            id="authored-plan-tools-v1",
            max_retries=0,
        )
        return self._toolset


def authored_plan_capability(client, *, enabled=typed_plans_enabled) -> AuthoredPlanTools:
    return AuthoredPlanTools(client, enabled=enabled)
