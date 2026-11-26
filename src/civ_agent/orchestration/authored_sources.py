"""Authored intent annotations derived from upstream-delivered tool messages."""

import copy
import hashlib
import json
import sqlite3
from collections import Counter
from collections.abc import Sequence
from typing import Annotated

from pydantic import Field, TypeAdapter, ValidationError, field_validator
from pydantic_ai.messages import (
    ModelMessage,
    ModelMessagesTypeAdapter,
    ModelRequest,
    ModelResponse,
    ToolCallPart,
    ToolReturnPart,
    UserPromptPart,
)
from pydantic_ai_harness.memory import MemoryStore, MemoryWriteResult
from pydantic_ai_harness.step_persistence import StepEvent

from civ_agent.orchestration.authored_memory import (
    MAX_DOCUMENT_CHARS,
    AuthoredMetadata,
    authored_prefix,
    document_kind,
    validate_document,
)
from civ_agent.orchestration.authored_plan_service import AuthoredDocumentReply
from civ_agent.orchestration.contracts import GatewayCall, GatewayJob, canonical
from civ_agent.orchestration.gateway_execution import operation_identity
from civ_agent.orchestration.store import JournalConflict, WorkflowStore
from civ_agent.orchestration.transcripts import bound_run
from civ_agent.protocol import JsonObject
from civ_agent.schemas import BoundaryModel, Identifier, NonnegativeInt


class ReadMemoryArguments(BoundaryModel):
    file: Annotated[str, Field(max_length=80, pattern=r"^(strategy\.md|city-[A-Za-z0-9._-]+\.md)$")]

    @field_validator("file", mode="before")
    @classmethod
    def native_filename(cls, value: object) -> object:
        if isinstance(value, str):
            value = value.strip()
            return value if value.endswith(".md") else value + ".md"
        return value


class ChoiceBaseline(BoundaryModel):
    id: Identifier
    authoredTurn: NonnegativeInt | None
    position: JsonObject | None


_BASELINES = TypeAdapter(dict[str, ChoiceBaseline])
_WRITE_RESULT = TypeAdapter(MemoryWriteResult)
_TYPED_READS = frozenset({"read_city_plan", "read_strategy", "review_investments"})


def _typed_filename(tool_name: str, arguments: JsonObject) -> str:
    if tool_name in {"read_strategy", "review_investments"} and not arguments:
        return "strategy.md"
    if tool_name == "read_city_plan" and set(arguments) == {"city"}:
        city = arguments["city"]
        if isinstance(city, str):
            return ReadMemoryArguments(file="city-" + city + ".md").file
    raise ValueError("AUTHORED_TYPED_READ_ARGUMENTS")


def _typed_source(file: str, part: ToolReturnPart) -> JsonObject | None:
    try:
        reply = AuthoredDocumentReply.model_validate(part.content, strict=True)
    except ValidationError:
        return None
    if (
        reply.error is not None
        or not reply.existed
        or reply.applied
        or reply.content is None
        or not reply.version
        or reply.id != file
    ):
        return None
    document = reply.content
    if (
        file == "strategy.md"
        and document.type != "strategy"
        or file != "strategy.md"
        and (document.type != "city_plan" or file != "city-" + document.city + ".md")
    ):
        return None
    body = document.model_dump(mode="json")
    return {
        "version": None,
        "category": document.type,
        "body": body,
        "source": {
            "file": file,
            "toolCallId": part.tool_call_id,
            "toolName": part.tool_name,
            "readVersion": reply.version,
            "documentSha256": hashlib.sha256(canonical(body).encode()).hexdigest(),
        },
    }


def project_authored_sources(messages: Sequence[ModelMessage], current_call_id: str) -> dict[str, JsonObject]:
    """Use only complete native reads the current provider response could see."""
    return _project_authored_sources(messages, current_call_id)[0]


def _project_authored_sources(
    messages: Sequence[ModelMessage], current_call_id: str
) -> tuple[dict[str, JsonObject], dict[str, tuple[str, str]]]:
    current = [
        index
        for index, message in enumerate(messages)
        if isinstance(message, ModelResponse)
        and any(isinstance(part, ToolCallPart) and part.tool_call_id == current_call_id for part in message.parts)
    ]
    if len(current) != 1:
        raise JournalConflict("AUTHORED_SOURCE_CALL_CONFLICT")
    response = messages[current[0]]
    assert isinstance(response, ModelResponse)
    selected = [
        part for part in response.parts if isinstance(part, ToolCallPart) and part.tool_call_id == current_call_id
    ]
    if len(selected) != 1 or selected[0].tool_name != "execute_commands":
        raise JournalConflict("AUTHORED_SOURCE_TOOL_CONFLICT")
    # Exclude the entire response, including an earlier-executed sibling read.
    prior = messages[: current[0]]
    calls = Counter(
        part.tool_call_id
        for message in prior
        if isinstance(message, ModelResponse)
        for part in message.parts
        if isinstance(part, ToolCallPart)
    )
    returns = Counter(
        part.tool_call_id
        for message in prior
        if isinstance(message, ModelRequest)
        for part in message.parts
        if isinstance(part, ToolReturnPart)
    )
    pending: dict[str, tuple[str, str, tuple[str, str] | None]] = {}
    writes: dict[str, tuple[str, str]] = {}
    candidates: dict[str, tuple[str, str]] = {}
    sources: dict[str, JsonObject] = {}
    for message in prior:
        if isinstance(message, ModelResponse):
            for part in message.parts:
                if (
                    not isinstance(part, ToolCallPart)
                    or part.tool_name not in {"read_memory", "write_memory"} | _TYPED_READS
                    or calls[part.tool_call_id] != 1
                ):
                    continue
                try:
                    arguments = part.args_as_dict(raise_if_invalid=True)
                    file = (
                        _typed_filename(part.tool_name, arguments)
                        if part.tool_name in _TYPED_READS
                        else ReadMemoryArguments.model_validate({"file": arguments.get("file", "MEMORY.md")}).file
                    )
                except (ValueError, AssertionError):
                    continue
                if ".." not in file:
                    # Capture the earlier ACK at provider-response issuance.
                    # A sibling write ACK cannot retroactively precede this read.
                    pending[part.tool_call_id] = file, part.tool_name, writes.get(file)
        elif isinstance(message, ModelRequest):
            for part in message.parts:
                if not isinstance(part, ToolReturnPart):
                    continue
                requested = pending.pop(part.tool_call_id, None)
                if (
                    requested is None
                    or returns[part.tool_call_id] != 1
                    or part.outcome != "success"
                    or part.tool_name != requested[1]
                ):
                    continue
                file, tool_name, prior_write = requested
                if tool_name in _TYPED_READS:
                    source = _typed_source(file, part)
                    if source is not None:
                        sources[file] = source
                        candidates[file] = source["source"]["readVersion"], part.tool_call_id
                    continue
                if tool_name == "write_memory":
                    try:
                        result = _WRITE_RESULT.validate_python(part.content, strict=True)
                    except ValidationError:
                        continue
                    if result["file"] == file:
                        writes[file] = result["version"], part.tool_call_id
                    continue
                if not isinstance(part.content, str) or len(part.content) > MAX_DOCUMENT_CHARS:
                    continue
                try:
                    document = validate_document(file, part.content)
                except ValueError:
                    continue
                if document is None:
                    continue
                sources[file] = {
                    "version": None,
                    "category": document.type,
                    "body": document.model_dump(),
                    "source": {
                        "file": file,
                        "toolCallId": part.tool_call_id,
                        "contentSha256": hashlib.sha256(part.content.encode()).hexdigest(),
                    },
                }
                if prior_write is None:
                    candidates.pop(file, None)
                else:
                    candidates[file] = prior_write
    return sources, candidates


def _nested_frontier(
    events: list[StepEvent], identity: GatewayJob, call: GatewayCall
) -> tuple[list[ModelMessage], str] | None:
    """Reconstruct only settled children preceding this exact dispatch.

    A program's unfinished parent has no nested metadata yet. Child evidence
    is durably recorded before dispatch and before returning each result; it
    supplies the causal frontier even if a later statement aborts the program.
    """
    started = [
        event
        for event in events
        if event.metadata.get("native_nested_version") == "1"
        and event.kind == "tool_call_started"
        and event.metadata.get("native_call_id") == call.call_id
        and event.metadata.get("native_operation_id")
        == operation_identity(identity.job, identity.scope.epoch, call.model_step, call.call_id)
    ]
    if not started:
        return None
    if len(started) != 1 or started[0].tool_name != "execute_commands" or started[0].tool_call_id is None:
        raise JournalConflict("AUTHORED_NESTED_CALL_CONFLICT")
    target = started[0]
    history: list[ModelMessage] = []
    for event in events:
        if event.metadata.get("native_nested_version") != "1":
            continue
        if event.metadata.get("scope") != canonical(identity.scope.model_dump(mode="json")):
            raise JournalConflict("AUTHORED_NESTED_SCOPE_CONFLICT")
        key = "nested_call" if event.kind == "tool_call_started" else "nested_result"
        if key not in event.metadata:
            raise JournalConflict("AUTHORED_NESTED_EVIDENCE_REQUIRED")
        recorded = ModelMessagesTypeAdapter.validate_json(event.metadata[key])
        if len(recorded) != 1 or not recorded[0].parts:
            raise JournalConflict("AUTHORED_NESTED_EVIDENCE_CONFLICT")
        part = recorded[0].parts[0]
        if (
            not isinstance(part, (ToolCallPart, ToolReturnPart))
            or part.tool_call_id != event.tool_call_id
            or part.tool_name != event.tool_name
            or any(not isinstance(extra, UserPromptPart) for extra in recorded[0].parts[1:])
        ):
            raise JournalConflict("AUTHORED_NESTED_EVIDENCE_CONFLICT")
        history.extend(recorded)
        if event is target:
            assert target.tool_call_id is not None
            return history, target.tool_call_id
    raise JournalConflict("AUTHORED_NESTED_FRONTIER_REQUIRED")


async def authored_sources(
    store: WorkflowStore,
    identity: GatewayJob,
    run_id: str,
    call: GatewayCall,
    authored_store: MemoryStore,
) -> dict[str, JsonObject]:
    """Bind canonical SDK reads to a registered native function-tool invocation.

    This grants authored-plan attribution only. It supplies no game observation,
    command permission, or write/CAS permission for a model's claimed references.
    Legacy reads need a prior native write ACK because read_memory delivers text
    only. Typed reads deliver their observed version explicitly. Both require
    current version/content/metadata matching before certifying the current
    version or attaching trusted Strategy baselines. A typed save without a
    later read delivers no document content and supplies no source.
    """
    steps = await bound_run(store, identity.scope, run_id, agent="player_role_v1")
    frontier = _nested_frontier(await steps.list_events(run_id=run_id), identity, call)
    sdk_call_id = frontier[1] if frontier else call.call_id
    effect = await steps.get_tool_effect(run_id=run_id, tool_call_id=sdk_call_id)
    if (
        effect is None
        or effect.tool_name != "execute_commands"
        or effect.status not in {"started", "completed"}
        or effect.idempotency_key
        != operation_identity(identity.job, identity.scope.epoch, call.model_step, call.call_id)
    ):
        raise JournalConflict("AUTHORED_SOURCE_EFFECT_CONFLICT")
    if frontier is not None:
        sources, candidates = _project_authored_sources(frontier[0], sdk_call_id)
    else:
        snapshot = await steps.latest_snapshot(run_id=run_id, include_interrupted=True)
        if snapshot is None:
            return {}
        sources, candidates = _project_authored_sources(snapshot.messages, sdk_call_id)
    for file, source in sources.items():
        source["source"]["runId"] = run_id
        if file not in candidates:
            continue
        try:
            current = await authored_store.read(authored_prefix(identity.scope) + file, max_chars=MAX_DOCUMENT_CHARS)
        except (OSError, TimeoutError, sqlite3.OperationalError):
            continue  # Delivered text remains evidence of authorship, never current state.
        if current is None:
            continue
        try:
            metadata = AuthoredMetadata.model_validate_strings(dict(current.metadata or {}))
        except ValidationError:
            raise JournalConflict("AUTHORED_SOURCE_METADATA_CONFLICT") from None
        if (metadata.game, metadata.player, metadata.epoch) != (
            identity.scope.game,
            identity.scope.player,
            identity.scope.epoch,
        ) or metadata.kind != document_kind(file):
            raise JournalConflict("AUTHORED_SOURCE_METADATA_CONFLICT")
        if current.truncated or current.version != candidates[file][0]:
            continue
        typed = source["source"].get("toolName") in _TYPED_READS
        if typed:
            try:
                document = validate_document(file, current.content)
            except ValueError:
                continue
            if (
                document is None
                or hashlib.sha256(canonical(document.model_dump(mode="json")).encode()).hexdigest()
                != source["source"]["documentSha256"]
            ):
                continue
        elif hashlib.sha256(current.content.encode()).hexdigest() != source["source"]["contentSha256"]:
            continue
        source["version"] = current.version
        if not typed:
            source["source"]["writeToolCallId"] = candidates[file][1]
        if source["category"] != "strategy":
            continue
        try:
            baselines = _BASELINES.validate_python(json.loads(metadata.decisionBaselines))
        except ValueError:
            raise JournalConflict("AUTHORED_SOURCE_BASELINE_CONFLICT") from None
        delivered_choices = {item["id"] for item in source["body"].get("decisions", [])}
        matched = {key: baseline.model_dump() for key, baseline in baselines.items() if key in delivered_choices}
        if matched:
            source["body"]["_decisionBaselines"] = copy.deepcopy(matched)
    with store.transaction(identity.scope):
        pass  # Recheck the epoch after all asynchronous reads.
    return sources
