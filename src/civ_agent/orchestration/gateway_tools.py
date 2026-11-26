"""Native function tools from canonical domain models and a permitted catalogue.

Only the invoker crosses into the trusted gateway. Dependencies contain opaque
identities; returned tool content contains receipts, never private gateway state.
"""

from collections.abc import Sequence
from dataclasses import replace
from functools import lru_cache, reduce
from operator import or_
from typing import Annotated, Any, ClassVar, Literal, Protocol, get_args

from pydantic import BaseModel, Field, RootModel, create_model, model_validator
from pydantic_ai import RunContext, Tool
from pydantic_ai.exceptions import CallDeferred, ModelRetry
from pydantic_ai.tools import ToolFuncContext

from civ_agent.decision_contracts import READS, compile_decision_contract
from civ_agent.delivery_options import DeliveryOptions
from civ_agent.game_commands import PlayerCommand, command_kinds, command_schema
from civ_agent.inspection_bundle import typed_bundle_model
from civ_agent.memory_records import Intent
from civ_agent.orchestration.contracts import GatewayDependencies, GatewayJob, GatewayReceipt, PlayerRole
from civ_agent.orchestration.peace_wait_contracts import approved_peace_wait
from civ_agent.orchestration.store import JournalConflict
from civ_agent.protocol import JsonObject
from civ_agent.schemas import BoundaryModel
from civ_agent.tool_schemas import CommandHelp
from civ_agent.war_state import FinalizeWarGoals, InitialWarGoals, ProposeWarGoals, ReadWarGoals, RespondWarGoals


def native_return_schema():
    """Small truthful transport shape; payload fields remain tool-specific data.

    Return schemas document results, never validate or confer authority. Error
    envelopes may omit decisionId; unexecuted rows deliberately lack a result.
    """
    return {
        "type": "object",
        "title": "NativeResult",
        "additionalProperties": True,
        "properties": {
            "decisionId": {"type": "string"},
            "receipts": {
                "type": "array",
                "items": {
                    "type": "object",
                    "title": "NativeReceipt",
                    "additionalProperties": True,
                    "properties": {
                        "operationId": {"type": "string"},
                        "tool": {"type": "string"},
                        "status": {"enum": ["confirmed", "rejected", "unexecuted"]},
                        "result": {"type": "object", "additionalProperties": True},
                        "reason": {"type": "string"},
                    },
                    "required": ["operationId", "status"],
                },
            },
            "unexecuted": {"type": "array", "items": {"type": "string"}},
            "error": {},
        },
        "required": ["receipts", "unexecuted"],
    }


async def describe_native_result(_ctx, definition):
    return replace(definition, return_schema=native_return_schema())


class GatewayInvoker(Protocol):
    async def __call__(
        self, deps: GatewayDependencies, call_id: str, model_step: int, decision: JsonObject, /
    ) -> GatewayReceipt: ...


def parameterize(form: Any, arguments: Any) -> Any:
    """Build runtime types for create_model without treating values as annotations."""
    return form[arguments]


def tagged_union(models, discriminator):
    return parameterize(Annotated, (reduce(or_, models), Field(discriminator=discriminator)))


def hidden(annotation, default):
    return parameterize(ClassVar, annotation), default


def command_type(schema: JsonObject, *, include_metadata=False, review_after=False):
    """Narrow canonical command variants while retaining their semantic validators."""
    allowed = command_kinds(schema)
    variants = []
    for model in get_args(get_args(PlayerCommand)[0]):
        selected = tuple(kind for kind in get_args(model.model_fields["kind"].annotation) if kind in allowed)
        if not selected:
            continue
        fields = {"kind": (parameterize(Literal, selected), ...)}
        if not include_metadata:
            fields["intent"] = hidden(Intent | None, None)
        if not review_after:
            fields["reviewAfter"] = hidden(bool, False)
        variants.append(
            create_model(
                "Native" + model.__name__,
                __base__=model,
                **fields,
            )
        )
    if not variants:
        raise ValueError("GATEWAY_COMMAND_CATALOGUE_EMPTY")
    return tagged_union(variants, "kind")


def read_arguments(name: str, model: type[BaseModel], catalogue: JsonObject) -> type[BaseModel]:
    handled = name == "chat_read" and "handledMessages" in catalogue["inputSchema"].get("properties", {})
    return _read_arguments(name, model, handled)


@lru_cache(maxsize=128)
def _read_arguments(name: str, model: type[BaseModel], handled: bool) -> type[BaseModel]:
    excluded: dict[str, Any] = {}
    if issubclass(model, DeliveryOptions):
        excluded.update(delivery=hidden(str, "auto"), full=hidden(bool, False))
    if name == "chat_read" and not handled:
        excluded["handledMessages"] = hidden(list[str], [])
    return create_model("Native" + model.__name__, __base__=model, **excluded) if excluded else model


def gateway_tools(
    purpose: PlayerRole,
    catalogue: Sequence[JsonObject],
    invoke: GatewayInvoker,
    *,
    include_controls: bool = False,
    command_catalogue: JsonObject | None = None,
    include_bundle_receipts: bool = False,
) -> list[Tool[Any]]:
    contract = compile_decision_contract(purpose, catalogue)
    entries = {entry["name"]: entry for entry in catalogue}
    tools: list[Tool[Any]] = []
    actions = []

    async def dispatch(ctx: RunContext[GatewayDependencies], decision: JsonObject) -> JsonObject:
        if ctx.deps.purpose != purpose or not ctx.tool_call_id:
            raise JournalConflict("GATEWAY_TOOL_CONTEXT_CONFLICT")
        model_step = ctx.deps.model_step_offset + ctx.run_step
        if decision.get("kind") == "act":
            approval = approved_peace_wait(
                GatewayJob(scope=ctx.deps.scope, job=ctx.deps.job, purpose=ctx.deps.purpose),
                ctx.tool_call_id,
                "act",
                decision,
                approved=ctx.tool_call_approved,
                metadata=ctx.tool_call_metadata,
            )
            if approval is not None:
                model_step, decision = approval.call.model_step, approval.call.decision
        execution = await invoke(ctx.deps, ctx.tool_call_id, model_step, decision)
        return execution.result

    def read_tool(name: str, model: type[BaseModel]) -> Tool:
        wrapped = issubclass(model, RootModel)
        fields: dict[str, Any] = {"arguments": (model, ...)} if wrapped else {}
        if name in contract.recoverable:
            fields["recoverBaseline"] = (Literal["missing", "mismatched"] | None, None)

        @model_validator(mode="before")
        @classmethod
        def legacy_envelope(cls, value):
            if isinstance(value, dict) and "arguments" in value:
                if set(value) <= {"arguments", "recoverBaseline"} and isinstance(value["arguments"], dict):
                    if "recoverBaseline" in value["arguments"]:
                        raise ValueError("Recovery metadata belongs outside legacy arguments")
                    return {**value["arguments"], **{key: item for key, item in value.items() if key != "arguments"}}
            return value

        validators: dict[str, Any] = {} if wrapped else {"legacy_envelope": legacy_envelope}
        request_model = create_model(
            "NativeRead_" + name,
            __base__=BoundaryModel if wrapped else model,
            __validators__=validators,
            **fields,
        )

        async def read(ctx, request):
            arguments = (request.arguments if wrapped else request).model_dump(by_alias=True, exclude_unset=True)
            if not wrapped:
                arguments.pop("recoverBaseline", None)
            operation = {"tool": name, "arguments": arguments}
            recovery = getattr(request, "recoverBaseline", None)
            if recovery is not None:
                operation["recoverBaseline"] = recovery
            return await dispatch(ctx, {"kind": "inspect", "operations": [operation]})

        # The SDK advertises a single model's fields directly. Keep the request
        # parameter and legacy envelope valid for recorded native tool calls.
        # RootModel unions retain their existing explicit object wrapper.
        read.__annotations__ = {
            "ctx": RunContext[GatewayDependencies],
            "request": request_model,
            "return": dict[str, Any],
        }
        return Tool(
            read,
            name=name,
            takes_ctx=True,
            max_retries=0,
            sequential=True,
            prepare=describe_native_result,
            description=(
                "Read private war episodes, goals, operations, escort assignments and messages with action='read'. "
                "For initial, propose, respond or finalize mutations, call act with operations containing "
                "{'tool': 'war_goals', 'arguments': {...}}."
                if name == "war_goals"
                else entries[name].get("description", f"Inspect permitted {name}.")
            ),
        )

    for name in sorted(contract.names):
        model = contract.models[name]
        if name == "war_goals":
            tools.append(read_tool(name, ReadWarGoals))
            if purpose not in {"gameplay", "military"}:
                continue
            model = RootModel[
                Annotated[
                    InitialWarGoals | ProposeWarGoals | RespondWarGoals | FinalizeWarGoals,
                    Field(discriminator="action"),
                ]
            ]
        elif name in READS:
            assert model is not None
            tools.append(read_tool(name, read_arguments(name, model, entries[name])))
            continue
        elif name == "player_command":
            fields: dict[str, Any] = {
                "command": (command_type(entries[name]["inputSchema"]), ...),
                "intent": (Intent | None, None),
            }
            if purpose == "military":
                fields["reviewAfter"] = (bool, False)
            model = create_model("NativePlayerCommandArguments", __base__=BoundaryModel, **fields)
        assert model is not None
        actions.append(
            create_model(
                "NativeOperation_" + name,
                __base__=BoundaryModel,
                tool=(parameterize(Literal, name), ...),
                arguments=(model, ...),
            )
        )

    if actions:
        operation = tagged_union(actions, "tool")

        def action_decision(operations):
            return {
                "kind": "act",
                "operations": [item.model_dump(by_alias=True, exclude_unset=True) for item in operations],
            }

        def validate_act(_ctx: RunContext[GatewayDependencies], operations) -> None:
            try:
                contract.parse(contract.encode(action_decision(operations)))
            except ValueError:
                raise ModelRetry("Action arguments must satisfy the permitted action contract.") from None

        async def act(ctx: RunContext[GatewayDependencies], operations):
            return await dispatch(ctx, action_decision(operations))

        act.__annotations__ = {
            "ctx": RunContext[GatewayDependencies],
            "operations": parameterize(Annotated, (parameterize(list, operation), Field(min_length=1, max_length=12))),
            "return": dict[str, Any],
        }
        act_function: ToolFuncContext[GatewayDependencies, ...] = act
        tools.append(
            Tool(
                act_function,
                name="act",
                takes_ctx=True,
                args_validator=validate_act,
                max_retries=0,
                sequential=True,
                prepare=describe_native_result,
                description="Execute 1–12 independent, fully decided actions in order. Inspect receipts before continuing; never repeat a confirmed action. Execution stops when prerequisites change.",
            )
        )

    def control(name: str, kind: str) -> Tool:
        async def call(ctx, arguments):
            result = await dispatch(
                ctx, {"kind": kind, "arguments": arguments.model_dump(by_alias=True, exclude_unset=True)}
            )
            if kind == "wait" and any(
                receipt.get("result", {}).get("economyWait", {}).get("state") == "waiting"
                for receipt in result.get("receipts", [])
            ):
                raise CallDeferred(metadata={"model_step": ctx.deps.model_step_offset + ctx.run_step})
            return result

        call.__annotations__ = {
            "ctx": RunContext[GatewayDependencies],
            "arguments": contract.models[name],
            "return": dict[str, Any],
        }
        return Tool(call, name=name, takes_ctx=True, max_retries=0, sequential=True)

    if "finish_role" in entries and "finish_role" in contract.models:
        tools.append(control("finish_role", "finish"))
    if "economy_wait" in entries and "economy_wait" in contract.models:
        tools.append(control("economy_wait", "wait"))
    if include_controls and "inspect_bundle" in entries:
        bundle_model = typed_bundle_model(
            tuple(
                (name, read_arguments(name, model, entries[name]))
                for name, model in sorted(contract.models.items())
                if name in READS and name in entries and model is not None
            )
        )

        async def inspect_bundle(ctx, request):
            operations = []
            for query in request.queries:
                if query.tool not in entries or query.tool not in READS:
                    raise ModelRetry("The inspection bundle contains a tool outside this role's catalogue.")
                body = query.arguments.model_dump(mode="json", by_alias=True, exclude_unset=True)
                body.pop("delivery", None)
                body.pop("full", None)
                operations.append({"tool": query.tool, "arguments": body})
            decision = {"kind": "inspect", "operations": operations}
            try:
                contract.parse(contract.encode(decision))
            except ValueError:
                raise ModelRetry("Every query must satisfy its canonical inspection schema.") from None
            result = await dispatch(ctx, decision)
            receipts = result.get("receipts", [])
            bundle = {
                "results": [
                    {
                        "tool": operation["tool"],
                        "arguments": operation["arguments"],
                        "result": receipt.get("result", {}),
                    }
                    for operation, receipt in zip(operations, receipts, strict=False)
                    if receipt.get("status") != "unexecuted"
                ],
                "remaining": len(result.get("unexecuted", [])),
                "decisionId": result.get("decisionId"),
                **({"error": result["error"]} if "error" in result else {}),
            }
            if include_bundle_receipts:
                bundle.update(
                    receiptVersion=1,
                    receiptEntries=[
                        {
                            "queryId": f"{ctx.tool_call_id}:query:{index}",
                            "queryIndex": index,
                            "tool": operation["tool"],
                            "arguments": operation["arguments"],
                            "status": receipts[index]["status"] if index < len(receipts) else "not_returned",
                            "receipt": receipts[index] if index < len(receipts) else None,
                        }
                        for index, operation in enumerate(operations)
                    ],
                    unexecuted=result.get("unexecuted", []),
                    feedback=result,
                )
            return bundle

        inspect_bundle.__annotations__ = {
            "ctx": RunContext[GatewayDependencies],
            "request": bundle_model,
            "return": dict[str, Any],
        }
        tools.append(
            Tool(
                inspect_bundle,
                takes_ctx=True,
                sequential=True,
                max_retries=2,
                description=(
                    "Read independent queries whose arguments are already known. Inner arguments use each canonical read schema; player_inspect uses subject/target directly, without another arguments wrapper. Returns receiptVersion=1 with legacy results plus receiptEntries for every requested query (queryId/queryIndex/tool/arguments/status/receipt), all unexecuted IDs, and full original native feedback. Missing receipts have status not_returned and receipt null. Use bundle_query with a unique queryId or exact tool/arguments; inspect status before result. Do not call result_data on the whole bundle."
                    if include_bundle_receipts
                    else "Read independent queries whose arguments are already known. Inner arguments use each canonical read schema; player_inspect uses subject/target directly, without another arguments wrapper. Returns a plain dictionary: results entries have tool/arguments/result, plus remaining/error. Check each entry and remaining before using data; do not call result_data on the whole bundle."
                ),
            )
        )
    if include_controls and "command_help" in entries:
        command_entry = command_catalogue or entries.get("player_command")

        async def command_help(request: CommandHelp):
            kinds = set(request.kinds)
            if command_entry is None or not kinds <= set(command_kinds(command_entry["inputSchema"])):
                return {"error": "COMMAND_NOT_PERMITTED", "applied": False}
            return {
                "description": command_entry.get("description", "Permitted native player commands."),
                "inputSchema": command_schema(kinds, military_review=purpose == "military"),
            }

        tools.append(Tool(command_help, takes_ctx=False, sequential=True, max_retries=2))
    return tools
