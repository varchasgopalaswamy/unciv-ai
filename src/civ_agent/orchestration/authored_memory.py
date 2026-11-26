"""Native authored notes behind the registered player's gateway authority."""

import json
import re
from pathlib import Path
from typing import Annotated, Literal

from pydantic import Field, JsonValue, TypeAdapter, ValidationError
from pydantic_ai import ModelRetry, RunContext, ToolFailed
from pydantic_ai.toolsets import FunctionToolset
from pydantic_ai_harness import Memory
from pydantic_ai_harness.memory import (
    MemoryConflictError,
    MemoryFile,
    MemoryMutation,
    MemoryOperation,
    MemoryOperationConflictError,
    MemorySearchResult,
    SqliteMemoryStore,
)
from sqlalchemy import select

from civ_agent import memory_schema as tables
from civ_agent.memory_records import CONTENT_MODELS, CityPlan, Strategy
from civ_agent.orchestration.authored_plan_service import (
    ChooseInvestmentDocument,
    DeleteAuthoredDocument,
    ReadAuthoredDocument,
    ReadInvestmentDocument,
    SaveAuthoredDocument,
    invoke_document,
)
from civ_agent.orchestration.contracts import Digest, GatewayDependencies, GatewayJob, Scope, digest
from civ_agent.orchestration.gateway_registry import job_key
from civ_agent.orchestration.store import JournalConflict
from civ_agent.protocol import JsonObject
from civ_agent.schema_briefs import schema_brief
from civ_agent.schemas import BoundaryModel, Identifier, NonnegativeInt

AUTHORED_AGENT = "notes"
MAX_DOCUMENT_CHARS = 8000
MAX_DOCUMENTS = 1000
_WIRE = TypeAdapter(MemoryFile | MemoryMutation | MemorySearchResult | list[str] | None)


def authored_namespace(scope: Scope) -> str:
    return f"{scope.game}/{scope.player}/{scope.epoch}"


def authored_prefix(scope: Scope) -> str:
    return authored_namespace(scope) + "/" + AUTHORED_AGENT + "/"


def contribution_prefix(identity: GatewayJob) -> str:
    return "contribution-" + digest(identity.job)[:16] + "-"


class AuthoredMetadata(BoundaryModel):
    format: Literal["authored-memory-v1"] = "authored-memory-v1"
    game: str
    player: str
    epoch: str
    originEpoch: str | None = None
    kind: Literal["strategy", "city_plan", "note", "contribution"] = "note"
    job: str
    purpose: Literal["gameplay", "economy", "military", "communication"]
    createdTurn: NonnegativeInt | None = None
    updatedTurn: NonnegativeInt | None = None
    state: Literal["active"] = "active"
    decisionBaselines: str = "{}"

    def strings(self):
        return {key: str(value) for key, value in self.model_dump(exclude_none=True).items()}


def document_kind(path: str) -> Literal["strategy", "city_plan", "note", "contribution"]:
    name = path.rsplit("/", 1)[-1]
    if name == "strategy.md":
        return "strategy"
    if name.startswith("city-"):
        return "city_plan"
    return "contribution" if name.startswith("contribution-") else "note"


def validate_document(path: str, content: str) -> Strategy | CityPlan | None:
    """Validate only game-specific documents; ordinary native memory is Markdown."""
    kind = document_kind(path)
    if kind == "strategy":
        return Strategy.model_validate_json(content)
    if kind == "city_plan":
        document = CityPlan.model_validate_json(content)
        if path.rsplit("/", 1)[-1] != "city-" + document.city + ".md":
            raise JournalConflict("AUTHORED_CITY_PATH_CONFLICT")
        return document
    return None


def decode_document(path: str, memory_file: MemoryFile) -> JsonObject:
    if memory_file.truncated or memory_file.metadata is None:
        raise JournalConflict("AUTHORED_DOCUMENT_INCOMPLETE")
    metadata = AuthoredMetadata.model_validate_strings(dict(memory_file.metadata))
    kind = document_kind(path)
    if metadata.kind != kind:
        raise JournalConflict("AUTHORED_DOCUMENT_KIND_CONFLICT")
    document = validate_document(path, memory_file.content)
    return {
        "version": memory_file.version,
        "category": kind,
        "body": {
            **(document.model_dump() if document is not None else {"type": "note", "text": memory_file.content}),
            "createdTurn": metadata.createdTurn,
            "updatedTurn": metadata.updatedTurn,
            "state": metadata.state,
            "basis": {"job": metadata.job, "purpose": metadata.purpose, "turn": metadata.updatedTurn},
            **({"_decisionBaselines": json.loads(metadata.decisionBaselines)} if kind == "strategy" else {}),
        },
    }


class Operation(BoundaryModel):
    id: Identifier
    fingerprint: Digest


class Read(BoundaryModel):
    action: Literal["read"]
    path: Annotated[str, Field(max_length=1024)]
    max_chars: Annotated[int, Field(ge=1, le=65536)]


class Lookup(BoundaryModel):
    action: Literal["get_operation"]
    operation: Operation


class Write(BoundaryModel):
    action: Literal["write"]
    path: Annotated[str, Field(max_length=1024)]
    content: Annotated[str, Field(max_length=MAX_DOCUMENT_CHARS)]
    expected_version: str | None
    operation: Operation


class Delete(BoundaryModel):
    action: Literal["delete"]
    path: Annotated[str, Field(max_length=1024)]
    expected_version: str | None
    operation: Operation


class Listing(BoundaryModel):
    action: Literal["list_paths"]
    prefix: Annotated[str, Field(max_length=1024)]
    limit: Annotated[int, Field(ge=1, le=MAX_DOCUMENTS + 1)]


class Search(BoundaryModel):
    action: Literal["search"]
    prefix: Annotated[str, Field(max_length=1024)]
    query: Annotated[str, Field(max_length=1000)]
    limit: Annotated[int, Field(ge=1, le=20)]
    max_files: Annotated[int, Field(ge=1, le=MAX_DOCUMENTS)]
    max_chars: Annotated[int, Field(ge=1, le=12000)]
    max_file_chars: Annotated[int, Field(ge=1, le=65536)]


class AuthoredMemoryRequest(BoundaryModel):
    version: Literal[1] = 1
    identity: GatewayJob
    call: Annotated[
        Read
        | Lookup
        | Write
        | Delete
        | Listing
        | Search
        | ReadAuthoredDocument
        | SaveAuthoredDocument
        | DeleteAuthoredDocument
        | ReadInvestmentDocument
        | ChooseInvestmentDocument,
        Field(discriminator="action"),
    ]


class AuthoredMemoryReply(BoundaryModel):
    result: JsonValue = None
    error: Literal["version_conflict", "operation_conflict", "invalid_document", "policy_rejected"] | None = None
    feedback: Annotated[str, Field(max_length=1600)] | None = None


class AuthoredDocumentError(ValueError):
    """Safe model feedback for invalid supplied content, never stored metadata."""


def document_feedback(error: ValidationError) -> str:
    names = set(CONTENT_MODELS) | {name for model in CONTENT_MODELS.values() for name in model.model_fields}
    fields = []
    for detail in error.errors(include_input=False, include_url=False)[:6]:
        path = ".".join(str(part) for part in detail["loc"] if part in names or type(part) is int) or "document"
        fields.append(path + ": " + detail["type"])
    return (
        "Document JSON does not match the permitted schema (" + "; ".join(fields) + "). "
        "Save exactly one complete JSON document with its required type and fields; strategy decision IDs must be unique. "
        "For an existing file, read it and use its exact full content as old_text with the complete replacement JSON as content. "
        "Omitting old_text appends, which cannot replace an existing JSON document."
    )


_POLICY_FEEDBACK = {
    "AUTHORED_PATH_CONFLICT": "Use a permitted role filename ending in .md, without subdirectories or '..'. Follow the role-specific filename instructions.",
    "AUTHORED_ROLE_CONFLICT": "This file or document type is not writable by your role. Follow the role-specific filename and document schemas; leave shared files unchanged when your role can only contribute suggestions.",
    "AUTHORED_CITY_OWNERSHIP_CONFLICT": "A city plan must name an owned city currently assigned to your role, in city-<exact-city-handle>.md. Read current player_turn and role coordination before choosing the city.",
    "AUTHORED_CITY_PATH_CONFLICT": "The city-plan filename must be city-<exact-city-handle>.md for the same city named in its JSON content.",
    "AUTHORED_CONTRIBUTION_IMMUTABLE": "Suggestions are immutable. Create a permitted non-city-plan document under your role's contribution filename prefix with a new short label.",
    "AUTHORED_DOCUMENT_LIMIT": "The private document limit is reached. Reuse an existing permitted file, or remove an obsolete file that your role may delete before creating another.",
}


class AuthoredMemoryService:
    def __init__(self, registry, database: Path):
        self.registry = registry
        self.store = SqliteMemoryStore(database=database)

    def _registered(self, identity, *, live):
        registry = self.registry
        with registry.gateway.memory.transaction() as db:
            record = registry._load(db, identity)
            if live:
                if record.retired:
                    raise JournalConflict("GATEWAY_JOB_RETIRED")
                if record.pending is not None:
                    raise JournalConflict("GATEWAY_RECONCILIATION_REQUIRED")
                registry._resume(db, record)
            row = db.execute(select(tables.jobs).where(tables.jobs.c.id == identity.job)).mappings().one()
            turn = registry.gateway.memory._meta(db, "gameTurn", row["game_turn"])
        return record, turn

    @staticmethod
    def _operation(identity, operation):
        return MemoryOperation(
            id=digest([identity.model_dump(mode="json"), operation.id]), fingerprint=operation.fingerprint
        )

    @staticmethod
    def _filename(identity, path):
        prefix = authored_prefix(identity.scope)
        if not path.startswith(prefix):
            raise JournalConflict("AUTHORED_NAMESPACE_CONFLICT")
        name = path.removeprefix(prefix)
        if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,79}", name) is None or ".." in name or not name.endswith(".md"):
            raise JournalConflict("AUTHORED_PATH_CONFLICT")
        return name

    async def _writer(self, identity, record):
        registry = self.registry
        job = registry.jobs.get(job_key(identity))
        if job is None:
            job = await registry.resolve(record)
            registry.jobs[job_key(identity)] = job
        registry.gateway.check(job)
        return job

    async def _city_owner(self, identity, job, city, name):
        coordinator = self.registry.gateway.role_coordinator
        view = job.observation
        if view is None:
            # A native action invalidates the working observation, including
            # the acting Economy job's own view. Validate ownership privately;
            # this read grants no delivery, inspection or decision credit.
            view = await job.connection.tool("player_turn", {})
            self.registry.gateway.check(job)
            if view.get("error") or view.get("epoch") != job.epoch or view.get("phase") != "ACTIVE":
                raise JournalConflict("AUTHORED_CITY_STATE_UNAVAILABLE")
        cities = {row["handle"] for row in view.get("cities", [])}
        owner = coordinator.owner(city) if coordinator is not None else "gameplay"
        if city not in cities or owner != identity.purpose or name != "city-" + city + ".md":
            raise JournalConflict("AUTHORED_CITY_OWNERSHIP_CONFLICT")

    async def _write(self, identity, call: Write, record, turn, operation, *, preserve_follow_through=False):
        name = self._filename(identity, call.path)
        try:
            document = validate_document(call.path, call.content)
        except ValidationError as error:
            raise AuthoredDocumentError(document_feedback(error)) from None
        current = await self.store.read(call.path, max_chars=MAX_DOCUMENT_CHARS)
        kind = document_kind(call.path)
        if kind == "contribution":
            if not name.startswith(contribution_prefix(identity)):
                raise JournalConflict("AUTHORED_ROLE_CONFLICT")
            if identity.purpose not in {"communication", "military"} or current is not None:
                raise JournalConflict("AUTHORED_CONTRIBUTION_IMMUTABLE")
        elif identity.purpose != "gameplay" and kind != "city_plan":
            raise JournalConflict("AUTHORED_ROLE_CONFLICT")
        job = await self._writer(identity, record)
        if kind == "city_plan":
            assert isinstance(document, CityPlan)
            city = document.city
            await self._city_owner(identity, job, city, name)
        previous = (
            AuthoredMetadata.model_validate_strings(dict(current.metadata or {})) if current is not None else None
        )
        if (
            current is None
            and len(await self.store.list_paths(authored_prefix(identity.scope), limit=MAX_DOCUMENTS + 1))
            >= MAX_DOCUMENTS
        ):
            raise JournalConflict("AUTHORED_DOCUMENT_LIMIT")
        metadata = AuthoredMetadata(
            game=identity.scope.game,
            player=identity.scope.player,
            epoch=identity.scope.epoch,
            originEpoch=previous.originEpoch if previous is not None else None,
            kind=kind,
            job=identity.job,
            purpose=identity.purpose,
            createdTurn=previous.createdTurn if previous is not None else turn,
            updatedTurn=turn,
        )
        if isinstance(document, Strategy):
            old_document = validate_document(call.path, current.content) if current is not None else None
            old_choices = (
                {choice.id: choice.model_dump() for choice in old_document.decisions}
                if isinstance(old_document, Strategy)
                else {}
            )
            old_baselines = json.loads(previous.decisionBaselines) if previous is not None else {}
            metadata.decisionBaselines = json.dumps(
                {
                    choice.id: old_baselines[choice.id]
                    if (
                        old_choices.get(choice.id) == choice.model_dump()
                        or preserve_follow_through
                        and {
                            k: v
                            for k, v in old_choices.get(choice.id, {}).items()
                            if k not in {"followThrough", "status"}
                        }
                        == {k: v for k, v in choice.model_dump().items() if k not in {"followThrough", "status"}}
                    )
                    and choice.id in old_baselines
                    else {
                        "id": digest([identity.model_dump(mode="json"), operation.id, choice.model_dump()]),
                        "authoredTurn": turn,
                        "position": None,
                    }
                    for choice in document.decisions
                },
                separators=(",", ":"),
                sort_keys=True,
            )
        return await self.store.write_with_metadata(
            call.path,
            call.content,
            expected_version=call.expected_version,
            operation=operation,
            metadata=metadata.strings(),
        )

    async def _delete(self, identity, call: Delete, record, operation):
        name = self._filename(identity, call.path)
        if identity.purpose not in {"gameplay", "economy"}:
            raise JournalConflict("AUTHORED_ROLE_CONFLICT")
        job = await self._writer(identity, record)
        if name.startswith("city-"):
            await self._city_owner(identity, job, name.removeprefix("city-").removesuffix(".md"), name)
        elif identity.purpose == "economy":
            raise JournalConflict("AUTHORED_ROLE_CONFLICT")
        return await self.store.delete(call.path, expected_version=call.expected_version, operation=operation)

    async def invoke(self, request: AuthoredMemoryRequest) -> AuthoredMemoryReply:
        identity, call = request.identity, request.call
        if isinstance(
            call,
            ReadAuthoredDocument
            | ReadInvestmentDocument
            | SaveAuthoredDocument
            | DeleteAuthoredDocument
            | ChooseInvestmentDocument,
        ):
            return AuthoredMemoryReply(result=(await invoke_document(self, identity, call)).model_dump(mode="json"))
        async with self.registry.dispatch:
            record, turn = await self.registry.gateway.io.call(self._registered, identity, live=False)
            operation = self._operation(identity, call.operation) if isinstance(call, (Lookup, Write, Delete)) else None
            try:
                if operation is not None:
                    replay = await self.store.get_operation(operation)
                    if isinstance(call, Lookup) or replay is not None:
                        return AuthoredMemoryReply(
                            result=_WIRE.dump_python(replay, mode="json") if replay is not None else None
                        )
                record, turn = await self.registry.gateway.io.call(self._registered, identity, live=True)
                if isinstance(call, (Write, Delete)) and self.registry.gateway.checkpoint_requested:
                    raise JournalConflict("AUTHORED_CHECKPOINT_IN_PROGRESS")
                if isinstance(call, Write):
                    result = await self._write(identity, call, record, turn, operation)
                elif isinstance(call, Delete):
                    result = await self._delete(identity, call, record, operation)
                elif isinstance(call, Read):
                    self._filename(identity, call.path)
                    result = await self.store.read(call.path, max_chars=call.max_chars)
                elif isinstance(call, Listing):
                    if call.prefix != authored_prefix(identity.scope):
                        raise JournalConflict("AUTHORED_NAMESPACE_CONFLICT")
                    result = await self.store.list_paths(call.prefix, limit=call.limit)
                elif isinstance(call, Search):
                    if call.prefix != authored_prefix(identity.scope):
                        raise JournalConflict("AUTHORED_NAMESPACE_CONFLICT")
                    result = await self.store.search(**call.model_dump(exclude={"action"}))
                else:
                    raise JournalConflict("AUTHORED_OPERATION_REQUIRED")
                return AuthoredMemoryReply(
                    result=_WIRE.dump_python(result, mode="json") if result is not None else None
                )
            except MemoryConflictError:
                return AuthoredMemoryReply(error="version_conflict")
            except MemoryOperationConflictError:
                return AuthoredMemoryReply(error="operation_conflict")
            except AuthoredDocumentError as error:
                return AuthoredMemoryReply(error="invalid_document", feedback=str(error))
            except JournalConflict as error:
                if feedback := _POLICY_FEEDBACK.get(str(error)):
                    return AuthoredMemoryReply(error="policy_rejected", feedback=feedback)
                raise


class GatewayMemoryStore:
    """Public native MemoryStore protocols over the authorized local service."""

    def __init__(self, client, identity: GatewayJob, *, typed_plans=False):
        self.client, self.identity, self.typed_plans = client, identity, typed_plans

    def _check_note_mutation(self, path):
        # Upstream Memory has already normalized its filename here. Keeping the
        # boundary at the store avoids copying its filename/extension rules.
        if self.typed_plans and document_kind(path) in {"strategy", "city_plan"}:
            raise ToolFailed(
                "The structured plan is unchanged. Use the typed city-plan or strategy tools; "
                "generic Memory edits only ordinary notes."
            )

    async def _call(self, action, **arguments):
        operation = arguments.get("operation")
        if operation is not None:
            arguments["operation"] = {"id": operation.id, "fingerprint": operation.fingerprint}
        request = AuthoredMemoryRequest.model_validate(
            {"identity": self.identity.model_dump(), "call": {"action": action, **arguments}}
        )
        # A local service client may itself be callable. Only a value without
        # `post` is a lazy getter, and resolution belongs inside this activity.
        client = self.client if hasattr(self.client, "post") else self.client()
        reply = AuthoredMemoryReply.model_validate(await client.post("memory", request))
        if reply.error == "version_conflict":
            raise MemoryConflictError("Authored memory version changed")
        if reply.error == "operation_conflict":
            raise MemoryOperationConflictError("Authored memory operation changed")
        if reply.error in {"invalid_document", "policy_rejected"}:
            raise ModelRetry(
                reply.feedback or "Correct the document using the permitted role filename and JSON schema."
            )
        return reply.result

    async def read(self, path: str, *, max_chars: int) -> MemoryFile | None:
        result = await self._call("read", path=path, max_chars=max_chars)
        return TypeAdapter(MemoryFile).validate_python(result) if result is not None else None

    async def get_operation(self, operation: MemoryOperation) -> MemoryMutation | None:
        result = await self._call("get_operation", operation=operation)
        return TypeAdapter(MemoryMutation).validate_python(result) if result is not None else None

    async def write(
        self, path: str, content: str, *, expected_version: str | None, operation: MemoryOperation | None = None
    ) -> MemoryMutation:
        self._check_note_mutation(path)
        result = await self._call(
            "write", path=path, content=content, expected_version=expected_version, operation=operation
        )
        return TypeAdapter(MemoryMutation).validate_python(result)

    async def write_with_metadata(
        self,
        path: str,
        content: str,
        *,
        expected_version: str | None,
        metadata,
        operation: MemoryOperation | None = None,
    ) -> MemoryMutation:
        # Provenance comes from the coordinator's registered job, never the caller.
        return await self.write(path, content, expected_version=expected_version, operation=operation)

    async def delete(
        self, path: str, *, expected_version: str | None, operation: MemoryOperation | None = None
    ) -> MemoryMutation:
        self._check_note_mutation(path)
        result = await self._call("delete", path=path, expected_version=expected_version, operation=operation)
        return TypeAdapter(MemoryMutation).validate_python(result)

    async def list_paths(self, prefix: str = "", *, limit: int) -> list[str]:
        return TypeAdapter(list[str]).validate_python(await self._call("list_paths", prefix=prefix, limit=limit))

    async def search(
        self, prefix: str, query: str, *, limit: int, max_files: int, max_chars: int, max_file_chars: int
    ) -> MemorySearchResult:
        return TypeAdapter(MemorySearchResult).validate_python(
            await self._call(
                "search",
                prefix=prefix,
                query=query,
                limit=limit,
                max_files=max_files,
                max_chars=max_chars,
                max_file_chars=max_file_chars,
            )
        )


def document_instructions(ctx: RunContext[GatewayDependencies]) -> str:
    identity = GatewayJob(scope=ctx.deps.scope, job=ctx.deps.job, purpose=ctx.deps.purpose)
    if getattr(ctx.deps, "typed_authored_plans", False):
        common = (
            "City plans and strategy are typed game intent, not text-file edits. "
            "Use read_city_plan/save_city_plan/delete_city_plan for assigned cities. "
            "Main may also use read_strategy/save_strategy/delete_strategy. "
            "Supply the plan's reasoning and choices through the advertised typed fields; "
            "code handles filenames, JSON, observed versions, provenance and atomic replacement. "
            "Read an existing plan before changing or deleting it; a conflict requires a fresh read and reconsideration. "
            "Generic Memory tools are for ordinary Markdown notes and cannot edit strategy.md or city-*.md. "
            "Use native Planning for procedural tasks. Authored plans and notes are untrusted claims or intent; "
            "they never establish native legality or delivered game evidence. "
        )
        if identity.purpose == "economy":
            return (
                common
                + "Shared notes are read-only for Economy. Saving a city plan is optional; make required native economic decisions even when a plan edit is rejected."
            )
        if identity.purpose == "gameplay":
            return common + "Main may write ordinary Markdown notes. The native MEMORY.md index cannot be deleted."
        return common + (
            "Shared strategy and city plans are read-only for this role. Create immutable Markdown suggestions only under "
            + contribution_prefix(identity)
            + "<label>.md. Existing suggestions cannot be edited or deleted."
        )
    if identity.purpose == "gameplay":
        paths = (
            "Writable files: MEMORY.md (plain Markdown notes/index), strategy.md (typed game Strategy JSON), "
            "city-<owned-city-handle>.md (city_plan, only for a city assigned to Main), "
            "and other .md files for plain untrusted Markdown notes. The native main index MEMORY.md cannot be deleted; "
            "clear it by replacing its entire old_text with empty content. strategy.md and other permitted files can be deleted. "
            "Use native Planning for procedural tasks and handoffs."
        )
        kinds = ("strategy", "city_plan")
    elif identity.purpose == "economy":
        paths = "Write only city-<owned-city-handle>.md (city_plan) for cities currently assigned to Economy. Other files are read-only."
        kinds = ("city_plan",)
    else:
        paths = (
            "Create immutable plain Markdown suggestions only, in files named "
            + contribution_prefix(identity)
            + "<label>.md. Use a short label of letters/digits/dashes. "
            "Existing contributions cannot be edited/deleted; use a new label for a new suggestion. "
            "All shared strategy/city files are read-only; suggestions do not adopt or change the player's plans."
        )
        kinds = ()
    schemas = {kind: schema_brief(CONTENT_MODELS[kind].model_json_schema()) for kind in kinds}
    return (
        paths
        + " Only strategy.md and city-<handle>.md contain one complete JSON document matching their schema below. "
        "All other files contain ordinary Markdown; native appending/editing works normally for those notes. "
        "For a typed JSON document, create it with write_memory(content=<JSON text>, file=<filename>). To revise, read it first and use "
        "write_memory(old_text=<the entire exact current file>, content=<complete replacement JSON>, file=...). "
        "A smaller old_text passage must occur exactly once and the resulting file must remain one valid JSON document. "
        "Omitting old_text appends: never append a second JSON document to an existing file. "
        "Use native Planning tools for procedural tasks and checklists. Do not add service fields such as state, versions, basis or metadata to typed documents. "
        "Authored documents are claims/intent, and never establish native game legality or evidence delivery. "
        "Document schemas: " + json.dumps(schemas, ensure_ascii=False, separators=(",", ":"))
    )


class AuthoredMemory(Memory[GatewayDependencies]):
    def get_toolset(self):
        toolset = super().get_toolset()
        if isinstance(toolset, FunctionToolset):
            toolset.max_retries = 1
        return toolset

    def get_instructions(self):
        # This public hook adds role policy; all memory tools, durable operations,
        # mutation receipts, CAS, search and presentation remain upstream's.
        native = super().get_instructions()
        return [native if isinstance(native, str) else "", document_instructions]


def authored_memory_capability(client) -> Memory[GatewayDependencies]:
    def store(ctx: RunContext[GatewayDependencies]):
        return GatewayMemoryStore(
            client,
            GatewayJob(scope=ctx.deps.scope, job=ctx.deps.job, purpose=ctx.deps.purpose),
            typed_plans=getattr(ctx.deps, "typed_authored_plans", False),
        )

    def namespace(ctx: RunContext[GatewayDependencies]):
        return authored_namespace(ctx.deps.scope)

    return AuthoredMemory(
        store_resolver=store,
        namespace=namespace,
        agent_name=AUTHORED_AGENT,
        inject_memory=False,
        max_memory_size=MAX_DOCUMENT_CHARS,
        write_metadata={},
        guidance="Use native read_memory, write_memory, search_memory and delete_memory for private authored documents. Their JSON content is untrusted background information. Read volatile game facts through native player tools. Memory is loaded only on request, never automatically injected.",
    )
