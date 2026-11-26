"""Typed game intent over native Memory CAS, receipts and scoped read observations."""

from copy import deepcopy
from typing import TYPE_CHECKING, Annotated, Literal, Self

from pydantic import Field, ValidationError, model_validator
from pydantic_ai_harness.memory import MemoryConflictError, MemoryFile, MemoryOperationConflictError

from civ_agent.memory_records import CityPlan, Source, Strategy, Text
from civ_agent.orchestration.contracts import Digest, GatewayJob, digest
from civ_agent.orchestration.gateway_registry import job_key
from civ_agent.orchestration.investment_records import InvestmentChoice
from civ_agent.orchestration.store import JournalConflict
from civ_agent.protocol import JsonObject
from civ_agent.schemas import BoundaryModel, Identifier

if TYPE_CHECKING:
    from civ_agent.orchestration.authored_memory import AuthoredMemoryService

type DocumentKind = Literal["city_plan", "strategy"]
type Document = Annotated[CityPlan | Strategy, Field(discriminator="type")]
_OBSERVATIONS = "authoredDocumentObservations"
type ModelStep = Annotated[int, Field(ge=1, strict=True)]


class DocumentTarget(BoundaryModel):
    document: DocumentKind
    city: Source | None = None
    model_step: ModelStep

    @model_validator(mode="after")
    def target(self) -> Self:
        if (self.document == "city_plan") != (self.city is not None):
            raise ValueError("An exact city is required only for a city plan")
        return self


class DocumentOperation(BoundaryModel):
    id: Identifier
    fingerprint: Digest


class ReadAuthoredDocument(DocumentTarget):
    action: Literal["read_document"] = "read_document"
    # Set only by the trusted sequential Code Mode adapter, never a tool input.
    program_delivered: bool = Field(default=False, exclude_if=lambda value: not value)


class ReadInvestmentDocument(DocumentTarget):
    action: Literal["read_investments"] = "read_investments"
    document: DocumentKind = "strategy"
    program_delivered: bool = Field(default=False, exclude_if=lambda value: not value)

    @model_validator(mode="after")
    def strategy_only(self):
        if self.document != "strategy":
            raise ValueError("Investment review reads the shared strategy")
        return self


class ChooseInvestmentDocument(BoundaryModel):
    action: Literal["choose_investment"] = "choose_investment"
    choice: InvestmentChoice
    assessment: Text | None = None
    replaceDecision: Source | None = None
    operation: DocumentOperation
    model_step: ModelStep


class SaveAuthoredDocument(BoundaryModel):
    action: Literal["save_document"] = "save_document"
    content: Document
    operation: DocumentOperation
    model_step: ModelStep


class ObservedDocument(BoundaryModel):
    version: Annotated[str, Field(min_length=1)] | None
    model_step: ModelStep
    program_delivered: bool = Field(default=False, exclude_if=lambda value: not value)


class DeleteAuthoredDocument(DocumentTarget):
    action: Literal["delete_document"] = "delete_document"
    operation: DocumentOperation


class AuthoredDocumentReply(BoundaryModel):
    id: str | None = None
    content: Document | None = None
    version: str | None = None
    existed: bool = False
    applied: bool = False
    replayed: bool = False
    error: Literal["read_required", "version_conflict", "policy_rejected"] | None = None
    feedback: Annotated[str, Field(max_length=1600)] | None = None
    code: str | None = Field(default=None, exclude_if=lambda value: value is None)
    recovery: JsonObject | None = Field(default=None, exclude_if=lambda value: value is None)
    progress: JsonObject | None = None
    planning: JsonObject | None = None


def _name(call: ReadAuthoredDocument | ReadInvestmentDocument | SaveAuthoredDocument | DeleteAuthoredDocument) -> str:
    if isinstance(call, SaveAuthoredDocument):
        return "city-" + call.content.city + ".md" if isinstance(call.content, CityPlan) else "strategy.md"
    return "city-" + str(call.city) + ".md" if call.document == "city_plan" else "strategy.md"


def _memory(record) -> JsonObject:
    memory = record.state.data.get("memory")
    if not isinstance(memory, dict):
        raise JournalConflict("AUTHORED_DELIVERY_STATE_CONFLICT")
    return memory


def _observations(memory: JsonObject) -> dict[str, ObservedDocument]:
    observations = memory.get(_OBSERVATIONS, {})
    if not isinstance(observations, dict):
        raise JournalConflict("AUTHORED_DELIVERY_STATE_CONFLICT")
    try:
        return {name: ObservedDocument.model_validate(value) for name, value in observations.items()}
    except ValidationError:
        raise JournalConflict("AUTHORED_DELIVERY_STATE_CONFLICT") from None


def _version(memory: JsonObject, name: str, model_step: int) -> tuple[bool, str | None]:
    observed = _observations(memory).get(name)
    if (
        observed is None
        or observed.model_step > model_step
        or (observed.model_step == model_step and not observed.program_delivered)
    ):
        return False, None
    return True, observed.version


def _observe_read(
    service: "AuthoredMemoryService",
    identity: GatewayJob,
    name: str,
    version: str | None,
    model_step: int,
    program_delivered: bool = False,
):
    registry = service.registry
    with registry.gateway.memory.transaction() as db:
        record = registry._load(db, identity)
        if record.retired:
            raise JournalConflict("GATEWAY_JOB_RETIRED")
        if record.pending is not None:
            raise JournalConflict("GATEWAY_RECONCILIATION_REQUIRED")
        registry._resume(db, record)
        memory = deepcopy(_memory(record))
        observations = _observations(memory)
        previous = observations.get(name)
        # A read executed as an earlier sibling is not yet visible to that
        # provider response. An unchanged version already observed by a prior
        # response keeps its earlier credit; a changed version does not.
        first_step = (
            min(previous.model_step, model_step) if previous is not None and previous.version == version else model_step
        )
        observations[name] = ObservedDocument(
            version=version, model_step=first_step, program_delivered=program_delivered
        )
        memory[_OBSERVATIONS] = {key: value.model_dump(mode="json") for key, value in observations.items()}
        record.state.data["memory"] = memory
        registry.gateway.memory._set(db, job_key(identity), record.model_dump(mode="json"))
    current = registry.jobs.get(job_key(identity))
    if current is not None:
        current.memory[_OBSERVATIONS] = deepcopy(memory[_OBSERVATIONS])


def _read_document(file: MemoryFile | None, path: str, identity: GatewayJob):
    from civ_agent.orchestration.authored_memory import AuthoredMetadata, decode_document

    if file is None:
        return None, AuthoredDocumentReply()
    try:
        metadata = AuthoredMetadata.model_validate_strings(dict(file.metadata or {}))
        if (metadata.game, metadata.player, metadata.epoch) != (
            identity.scope.game,
            identity.scope.player,
            identity.scope.epoch,
        ):
            raise JournalConflict("AUTHORED_METADATA_SCOPE_CONFLICT")
        decoded = decode_document(path, file)
        content = (
            CityPlan.model_validate_json(file.content)
            if decoded["category"] == "city_plan"
            else Strategy.model_validate_json(file.content)
        )
    except ValidationError:
        raise JournalConflict("AUTHORED_DOCUMENT_CORRUPT") from None
    return decoded, AuthoredDocumentReply(content=content, version=file.version, existed=True)


async def invoke_document(
    service: "AuthoredMemoryService",
    identity: GatewayJob,
    call: ReadAuthoredDocument
    | ReadInvestmentDocument
    | SaveAuthoredDocument
    | DeleteAuthoredDocument
    | ChooseInvestmentDocument,
) -> AuthoredDocumentReply:
    """Authorize and apply one semantic request through the native MemoryStore.

    CAS observations are scoped job state, separate from source evidence. Native
    MemoryOperation receipts return completed retries before live-job checks;
    no second transport call or application mutation journal is required.
    """
    from civ_agent.orchestration.authored_memory import (
        _POLICY_FEEDBACK,
        MAX_DOCUMENT_CHARS,
        Delete,
        Operation,
        Write,
        authored_prefix,
    )

    if isinstance(call, ChooseInvestmentDocument):
        return await choose_investment_document(service, identity, call)
    registry = service.registry
    async with registry.dispatch:
        record, turn = await registry.gateway.io.call(service._registered, identity, live=False)
        path = authored_prefix(identity.scope) + _name(call)
        try:
            name = service._filename(identity, path)
            operation = None
            if not isinstance(call, ReadAuthoredDocument | ReadInvestmentDocument):
                # CAS is chosen below from what this job already saw. It is not
                # semantic input: replay must retain its receipt after a reread.
                semantic = call.model_dump(mode="json", exclude={"operation", "model_step"})
                operation = service._operation(
                    identity,
                    Operation(id=call.operation.id, fingerprint=digest([path, semantic, call.operation.fingerprint])),
                )
                replay = await service.store.get_operation(operation)
                if replay is not None:
                    return AuthoredDocumentReply(
                        id=name, version=replay.version, existed=replay.existed, applied=True, replayed=True
                    )
            record, turn = await registry.gateway.io.call(service._registered, identity, live=True)
            if isinstance(call, ReadAuthoredDocument | ReadInvestmentDocument):
                file = await service.store.read(path, max_chars=MAX_DOCUMENT_CHARS)
                _, reply = _read_document(file, path, identity)
                await registry.gateway.io.call(
                    _observe_read, service, identity, name, reply.version, call.model_step, call.program_delivered
                )
                reply.id = name
                if isinstance(call, ReadInvestmentDocument):
                    reply.progress = await registry.gateway.io.call(investment_progress, service, record, turn)
                return reply
            if registry.gateway.checkpoint_requested:
                raise JournalConflict("AUTHORED_CHECKPOINT_IN_PROGRESS")
            job = await service._writer(identity, record)
            if name == "strategy.md":
                if identity.purpose != "gameplay":
                    raise JournalConflict("AUTHORED_ROLE_CONFLICT")
            else:
                if isinstance(call, SaveAuthoredDocument):
                    assert isinstance(call.content, CityPlan)
                    city = call.content.city
                else:
                    city = call.city
                await service._city_owner(identity, job, city, name)
            seen, version = _version(_memory(record), name, call.model_step)
            if not seen:
                return AuthoredDocumentReply(
                    id=name,
                    error="read_required",
                    feedback="Read the current structured document, then reconsider it in a later model response before saving or deleting. No change was applied.",
                )
            assert operation is not None
            try:
                mutation = (
                    Write(
                        action="write",
                        path=path,
                        content=call.content.model_dump_json(),
                        expected_version=version,
                        operation=Operation(id=call.operation.id, fingerprint=operation.fingerprint),
                    )
                    if isinstance(call, SaveAuthoredDocument)
                    else Delete(
                        action="delete",
                        path=path,
                        expected_version=version,
                        operation=Operation(id=call.operation.id, fingerprint=operation.fingerprint),
                    )
                )
            except ValidationError:
                return AuthoredDocumentReply(
                    id=name,
                    error="policy_rejected",
                    feedback="The structured document exceeds its storage limits. Shorten the plan; no change was applied.",
                )
            result = (
                await service._write(identity, mutation, record, turn, operation)
                if isinstance(mutation, Write)
                else await service._delete(identity, mutation, record, operation)
            )
            return AuthoredDocumentReply(
                id=name, version=result.version, existed=result.existed, applied=True, replayed=result.replayed
            )
        except MemoryConflictError:
            return AuthoredDocumentReply(
                id=_name(call),
                error="version_conflict",
                feedback="The plan changed after it was read. Read it again before saving.",
            )
        except MemoryOperationConflictError:
            raise JournalConflict("AUTHORED_OPERATION_CONFLICT") from None
        except JournalConflict as error:
            if feedback := _POLICY_FEEDBACK.get(str(error)):
                return AuthoredDocumentReply(error="policy_rejected", feedback=feedback)
            raise


def investment_progress(service, record, turn):
    """Only own retained receipts and an already delivered overview, never a native read."""
    memory = service.registry.gateway.memory
    with memory.transaction() as db:
        actions = memory._action_facts(db, max(0, turn - 4), turn)
    kept = actions[-32:]
    result = {
        "basis": "Historical own native receipts; no new native observation or inspection credit.",
        "asOfTurn": turn,
        "receipts": [
            {key: action[key] for key in ("receipt", "turn", "action", "commandStatus") if key in action}
            for action in kept
        ],
        "omittedEarlierReceipts": max(0, len(actions) - len(kept)),
        "authority": "A receipt proves its native operation, not completion of an authored objective or task.",
    }
    snapshot = _memory(record)
    from civ_agent.orchestration.investment_quotes import quote_sources

    result["quoteSources"] = quote_sources(snapshot)
    overview = snapshot.get("records", {}).get("observation:overview")
    if overview and snapshot.get("delivered", {}).get("observation:overview") == overview.get("version"):
        result["decisionFeedback"] = memory.decision_feedback(overview["body"])
    return result


async def choose_investment_document(service, identity, call: ChooseInvestmentDocument):
    """Merge one explicit choice with delivered CAS and the existing Memory receipt.

    The immutable semantic request is checked before reading current content, so
    a lost acknowledgement cannot replay an edit or absorb later plan changes.
    """
    from civ_agent.orchestration.authored_memory import (
        _POLICY_FEEDBACK,
        MAX_DOCUMENT_CHARS,
        Operation,
        Write,
        authored_prefix,
    )

    registry = service.registry
    async with registry.dispatch:
        await registry.gateway.io.call(service._registered, identity, live=False)
        if identity.purpose != "gameplay":
            raise JournalConflict("AUTHORED_ROLE_CONFLICT")
        path = authored_prefix(identity.scope) + "strategy.md"
        semantic = call.model_dump(mode="json", exclude={"operation", "model_step"})
        operation = service._operation(
            identity, Operation(id=call.operation.id, fingerprint=digest([path, semantic, call.operation.fingerprint]))
        )
        try:
            replay = await service.store.get_operation(operation)
            if replay is not None:
                return AuthoredDocumentReply(
                    id="strategy.md", version=replay.version, existed=replay.existed, applied=True, replayed=True
                )
            record, turn = await registry.gateway.io.call(service._registered, identity, live=True)
            if registry.gateway.checkpoint_requested:
                raise JournalConflict("AUTHORED_CHECKPOINT_IN_PROGRESS")
            await service._writer(identity, record)
            seen, version = _version(_memory(record), "strategy.md", call.model_step)
            if not seen:
                return AuthoredDocumentReply(
                    error="read_required", feedback="Review investments first; no strategy edit was applied."
                )
            current = await service.store.read(path, max_chars=MAX_DOCUMENT_CHARS)
            _, reply = _read_document(current, path, identity)
            if reply.version != version:
                return AuthoredDocumentReply(
                    error="version_conflict", feedback="The strategy changed after review; reread and reconsider."
                )
            from civ_agent.orchestration.investment_quotes import quote_error

            if error := quote_error(call.choice, _memory(record)):
                return AuthoredDocumentReply(error="policy_rejected", feedback=error)
            strategy = (
                reply.content
                if isinstance(reply.content, Strategy)
                else Strategy(type="strategy", assessment=call.assessment or call.choice.objective)
            )
            choices = [item for item in strategy.decisions if item.id not in {call.choice.id, call.replaceDecision}]
            choices.append(call.choice.decision())
            updated = strategy.model_copy(
                update={
                    "decisions": choices,
                    "assessment": call.assessment if call.assessment is not None else strategy.assessment,
                }
            )
            content = updated.model_dump_json()
            if len(content) > MAX_DOCUMENT_CHARS:
                return AuthoredDocumentReply(
                    id="strategy.md",
                    error="policy_rejected",
                    code="INVESTMENT_DOCUMENT_FULL",
                    feedback="The strategy document exceeds its character limit; no edit applied.",
                    recovery={
                        "instruction": "Shorten the strategy or explicitly replace a choice you no longer need to track. Existing choices were preserved.",
                        "reads": [{"tool": "review_investments", "arguments": {}}],
                        "limits": [{"fields": ["content"], "maximum": MAX_DOCUMENT_CHARS, "received": len(content)}],
                    },
                )
            mutation = Write(
                action="write",
                path=path,
                content=content,
                expected_version=version,
                operation=Operation(id=call.operation.id, fingerprint=operation.fingerprint),
            )
            saved = await service._write(identity, mutation, record, turn, operation, preserve_follow_through=True)
            return AuthoredDocumentReply(
                id="strategy.md", version=saved.version, existed=saved.existed, applied=True, replayed=saved.replayed
            )
        except MemoryConflictError:
            return AuthoredDocumentReply(
                error="version_conflict",
                feedback="The strategy changed; review and reconsider without replaying native actions.",
            )
        except MemoryOperationConflictError:
            raise JournalConflict("AUTHORED_OPERATION_CONFLICT") from None
        except ValidationError:
            return AuthoredDocumentReply(
                error="policy_rejected",
                feedback="Shorten this choice to fit the existing strategy document; no edit applied.",
            )
        except JournalConflict as error:
            if feedback := _POLICY_FEEDBACK.get(str(error)):
                return AuthoredDocumentReply(error="policy_rejected", feedback=feedback)
            raise
