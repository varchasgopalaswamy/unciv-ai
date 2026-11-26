"""Typed, read-only tools and validation for frozen strategic review inputs."""

import copy
from collections.abc import Callable
from dataclasses import replace
from typing import Any

from pydantic import BaseModel, TypeAdapter, ValidationError
from pydantic_ai import ModelRetry, RunContext, Tool
from pydantic_ai.messages import ModelMessage, ModelRequest, ToolCallPart, ToolReturnPart, UserPromptPart

from civ_agent.decision_contracts import NATIVE_MODELS
from civ_agent.orchestration.contracts import ReviewRequest, canonical, digest
from civ_agent.orchestration.providers import ProviderClients
from civ_agent.orchestration.store import JournalConflict, WorkflowStore
from civ_agent.protocol import JsonObject
from civ_agent.review_grounding import ReviewEvidence
from civ_agent.strategic_review import (
    REVIEW_READ_TOOLS,
    FrozenReadArgumentError,
    StrategicAdvice,
    advice_prompt,
    frozen_read,
)
from civ_agent.tool_schemas import tool_model


class StableEvidence(ReviewEvidence):
    """Source labels survive activity retries without minting additional evidence."""

    def __init__(self, inputs: JsonObject, *, namespace: str = ""):
        super().__init__(inputs)
        self.namespace = namespace

    def deliver(self, name: str, data: JsonObject | list, arguments: JsonObject | None = None) -> JsonObject:
        payload = {"name": name, "arguments": arguments or {}, "data": data}
        fingerprint = digest(payload)
        source = "s" + digest([self.namespace, fingerprint])[:32]
        self.sources[source] = {
            "id": source,
            "name": name,
            "observedTurn": self.turn,
            "sha256": fingerprint,
            **copy.deepcopy(payload),
        }
        self.delivered[fingerprint] = source
        return {"source": source, "data": copy.deepcopy(data)}


class ReviewServices:
    def __init__(self, store: WorkflowStore, clients: ProviderClients, *, model_factory: Callable | None = None):
        self.store, self.clients, self.model_factory = store, clients, model_factory

    def inputs(self, request: ReviewRequest) -> JsonObject:
        value = self.store.get(request.scope, request.inputs)
        if value["observation"]["turn"] != value["reads"]["player_turn"]["turn"]:
            raise ValueError("STRATEGIC_REVIEW_POSITION_MISMATCH")
        return value

    def prepare(self, request: ReviewRequest, *, guides_in_user=True):
        inputs = self.inputs(request)
        evidence = StableEvidence(inputs, namespace=request.scope.identity("evidence", "review"))
        prompt = advice_prompt(
            inputs,
            evidence=evidence,
            strategy_guide=request.strategy_guide,
            civilization_guide=request.civilization_guide,
            guides_in_user=guides_in_user,
        )
        return inputs, evidence, prompt

    def read(self, request: ReviewRequest, name: str, arguments: BaseModel) -> JsonObject:
        inputs = self.inputs(request)
        supplied = arguments.model_dump()
        try:
            result = frozen_read(inputs, name, supplied)
        except FrozenReadArgumentError as error:
            raise ModelRetry(
                f"{error.tool}: invalid arguments for the frozen review position. "
                + (f"Schema errors: {error.diagnostics}. " if error.diagnostics else "")
                + "Follow the advertised tool schema and choose only options present in the supplied frozen observations."
            ) from error
        evidence = StableEvidence(inputs, namespace=request.scope.identity("evidence", "review"))
        return evidence.deliver(name, result, supplied)


def read_tool(name: str, *, worker: ReviewServices | None = None) -> Tool[ReviewRequest]:
    arguments_model = NATIVE_MODELS.get(name) or tool_model(name, "review")

    async def read(ctx, arguments):
        if worker is None:
            raise ValueError("REVIEW_SERVICES_REQUIRED")
        try:
            return worker.read(ctx.deps, name, arguments)
        except ValidationError as error:
            # Caller validation already became FrozenReadArgumentError/ModelRetry.
            # The SDK treats raw ValidationError as correctable tool input even
            # when it came from trusted projection, storage or evidence code.
            raise JournalConflict("REVIEW_INTERNAL_VALIDATION_FAILED") from error

    # Canonical Pydantic argument models remain the sole schema/validation source.
    read.__annotations__ = {"ctx": RunContext[ReviewRequest], "arguments": arguments_model, "return": dict[str, Any]}
    return Tool(read, name=name, takes_ctx=True, description=f"Read frozen permitted {name} with source provenance.")


_READ_LEDGER = "civ_review_delivered_v1"
_CALL = TypeAdapter(ToolCallPart)
_RETURN = TypeAdapter(ToolReturnPart)


def _delivered_parts(messages):
    for message in messages:
        for part in message.parts:
            yield part
            if not isinstance(part, ToolReturnPart) or part.outcome != "success" or not isinstance(part.metadata, dict):
                continue
            calls, returns = part.metadata.get("tool_calls", {}), part.metadata.get("tool_returns", {})
            if not isinstance(calls, dict) or not isinstance(returns, dict):
                raise ValueError("REVIEW_NESTED_EVIDENCE_INVALID")
            for identifier, value in calls.items():
                call = _CALL.validate_python(value)
                if call.tool_call_id != identifier:
                    raise ValueError("REVIEW_NESTED_CALL_BINDING_CONFLICT")
                yield call
                if identifier in returns:
                    returned = _RETURN.validate_python(returns[identifier])
                    if returned.tool_call_id != identifier or returned.tool_name != call.tool_name:
                        raise ValueError("REVIEW_NESTED_RETURN_BINDING_CONFLICT")
                    yield returned


def delivered_reads(messages: list[ModelMessage], *, include_defaults: bool = False) -> list[JsonObject]:
    parts = list(_delivered_parts(messages))
    returned = {
        (part.tool_name, part.tool_call_id)
        for part in parts
        if isinstance(part, ToolReturnPart)
        and part.outcome == "success"
        and isinstance(part.content, dict)
        and isinstance(part.content.get("source"), str)
        and "data" in part.content
    }
    # Synthetic returns for skipped tools are not observations.
    reads = []
    for message in messages:
        ledger = message.metadata.get(_READ_LEDGER) if message.metadata else None
        if ledger is None:
            continue
        if (
            not isinstance(ledger, dict)
            or ledger.get("version") != 1
            or ledger.get("sha256") != digest(ledger.get("reads"))
        ):
            raise ValueError("REVIEW_DELIVERED_LEDGER_CONFLICT")
        for record in ledger["reads"]:
            name = record["name"]
            if name not in REVIEW_READ_TOOLS - {"inspect_bundle"}:
                raise ValueError("REVIEW_READ_ONLY")
            validated = read_tool(name).function_schema.validator.validate_python(record["arguments"])
            reads.append(
                {"name": name, "arguments": validated["arguments"].model_dump(exclude_unset=not include_defaults)}
            )
    for part in parts:
        if (
            not isinstance(part, ToolCallPart)
            or (part.tool_name, part.tool_call_id) not in returned
            or part.tool_name not in REVIEW_READ_TOOLS - {"inspect_bundle"}
        ):
            continue
        # Single-model tool parameters are flattened in the advertised SDK
        # schema. Reuse its validator for flat calls, wrapped replay inputs,
        # and omitted optional fields, exactly as the native tool does.
        validated = read_tool(part.tool_name).function_schema.validator.validate_python(part.args_as_dict())
        reads.append(
            {"name": part.tool_name, "arguments": validated["arguments"].model_dump(exclude_unset=not include_defaults)}
        )
    return list({canonical(read): read for read in reads}.values())


def retain_delivered_reads(messages: list[ModelMessage]) -> list[ModelMessage]:
    """Carry proven reads in private metadata; summary prose never grants evidence."""
    reads = delivered_reads(messages)
    if not reads:
        return messages
    for index, message in enumerate(messages):
        if isinstance(message, ModelRequest) and any(isinstance(part, UserPromptPart) for part in message.parts):
            metadata = {
                **(message.metadata or {}),
                _READ_LEDGER: {"version": 1, "reads": reads, "sha256": digest(reads)},
            }
            return [*messages[:index], replace(message, metadata=metadata), *messages[index + 1 :]]
    raise ValueError("REVIEW_FIRST_USER_REQUIRED")


def validate_report(
    request: ReviewRequest, report: StrategicAdvice, reads: list[JsonObject], *, worker: ReviewServices | None = None
) -> JsonObject:
    if worker is None:
        raise ValueError("REVIEW_SERVICES_REQUIRED")
    inputs, evidence, _ = worker.prepare(request)
    for read in reads:
        name, arguments = read["name"], read["arguments"]
        if name not in REVIEW_READ_TOOLS - {"inspect_bundle"}:
            raise ValueError("REVIEW_READ_ONLY")
        model = NATIVE_MODELS.get(name) or tool_model(name, "review")
        supplied = model.model_validate(arguments).model_dump()
        evidence.deliver(name, frozen_read(inputs, name, supplied), supplied)
    from civ_agent.strategic_review import parse_advice

    return parse_advice(canonical(report.model_dump()), inputs["observation"], evidence=evidence)
