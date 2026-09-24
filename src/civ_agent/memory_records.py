"""Validated agent-authored memory and intent. Operational metadata is service owned."""

from typing import Annotated, Literal

from pydantic import Field, ValidationError, model_validator

from civ_agent.model_view import model_view
from civ_agent.protocol import JsonObject
from civ_agent.schemas import BoundaryModel, NonnegativeInt, argument_issues, argument_shape

Text = Annotated[str, Field(min_length=1, max_length=2000)]
Source = Annotated[str, Field(min_length=1, max_length=160)]
Sources = Annotated[
    list[Source],
    Field(
        max_length=20,
        description="At most 20 relevant citations for this revision. Replace obsolete support; earlier revisions retain their citations.",
    ),
]
Retirement = Literal["completed", "abandoned", "superseded", "retracted", "expired", "irrelevant"]
MEMORY_TYPES = {"soul", "plan", "belief", "note"}


class Assumption(BoundaryModel):
    text: Text
    source: str = Field(default="", max_length=160)


class Content(BoundaryModel):
    subjects: Annotated[list[Source], Field(max_length=20)] = Field(default_factory=list)
    priority: Annotated[int, Field(ge=1, le=5)] = 3
    expiresTurn: NonnegativeInt | None = None


class Plan(Content):
    type: Literal["plan"]
    objective: Text
    approach: Text
    successCriteria: Text
    reviewAfterTurn: NonnegativeInt | None = None
    assumptions: Annotated[list[Assumption], Field(max_length=20)] = Field(default_factory=list)
    sources: Sources = Field(default_factory=list)


class Belief(Content):
    type: Literal["belief"]
    claim: Text
    uncertainty: Literal["low", "medium", "high"]
    sources: Sources = Field(default_factory=list)
    contradictingSources: Sources = Field(default_factory=list)


class Note(Content):
    type: Literal["note"]
    text: Text
    plan: str = Field(
        default="",
        max_length=160,
        description="Optional historical plan ID. Unread references are explicitly unverified. Omit when unrelated; never put prose here.",
    )


class Soul(Content):
    type: Literal["soul"]
    text: Text


RecordContent = Annotated[Plan | Belief | Note | Soul, Field(discriminator="type")]
CONTENT_MODELS = {"plan": Plan, "belief": Belief, "note": Note, "soul": Soul}


class Create(BoundaryModel):
    op: Literal["create"]
    content: RecordContent


class Retire(BoundaryModel):
    op: Literal["retire"]
    id: Source
    reason: Retirement
    assessment: str = Field(default="", max_length=2000)


class Amend(BoundaryModel):
    op: Literal["amend"]
    id: Source
    fields: Annotated[
        JsonObject,
        Field(
            min_length=1,
            max_length=12,
            description="Only changed content fields from a record you read. Omitted fields are retained. Lists replace the old list; they never append. Type and service metadata cannot be changed.",
        ),
    ]


class MemoryUpdate(BoundaryModel):
    changes: Annotated[list[Annotated[Create | Retire | Amend, Field(discriminator="op")]], Field(max_length=20)]


class MemoryContributions(BoundaryModel):
    """Communication may append contributions, never revise the shared notebook."""

    changes: Annotated[list[Create], Field(max_length=20)]


def source_limits(content: JsonObject, prefix: list) -> list[JsonObject]:
    return [
        {"fields": [*prefix, field], "maximum": 20, "received": len(content[field])}
        for field in ("sources", "contradictingSources")
        if isinstance(content.get(field), list) and len(content[field]) > 20
    ]


class MemoryContentError(ValueError):
    def __init__(self, details: JsonObject):
        super().__init__("INVALID_ARGUMENT")
        self.details = details


def amended_content(previous: JsonObject, fields: JsonObject) -> JsonObject:
    """Validate the complete revision before any writes, including retained fields."""
    model = CONTENT_MODELS[previous["type"]]
    if "type" in fields:
        raise ValueError("TYPE_CHANGE_REQUIRES_NEW_RECORD")
    content = {**{key: previous[key] for key in model.model_fields if key in previous}, **fields}
    try:
        return model.model_validate(content).model_dump()
    except ValidationError as error:
        raise MemoryContentError(
            {
                "invalidFields": argument_issues(error, model.model_json_schema()),
                "limits": source_limits(content, ["fields"]),
                "instruction": "Amend only content fields. Lists replace existing lists. Keep at most 20 relevant citations per source list; historical revisions retain earlier citations. No part of this edit was applied.",
            }
        ) from None


def memory_argument_error(arguments: JsonObject, error: ValidationError) -> JsonObject:
    shapes = {}
    limits = []
    changes = arguments.get("changes")
    for index, change in enumerate(changes if isinstance(changes, list) else []):
        content = change.get("content") if isinstance(change, dict) else None
        kind = content.get("type") if isinstance(content, dict) else None
        if isinstance(content, dict):
            limits.extend(source_limits(content, ["changes", index, "content"]))
        if isinstance(kind, str) and kind in CONTENT_MODELS:
            schema = CONTENT_MODELS[kind].model_json_schema()
            shapes[kind] = argument_shape(schema, schema.get("$defs", {}))
    return {
        "error": "INVALID_ARGUMENT",
        "applied": False,
        "recoverable": True,
        "invalidFields": argument_issues(error, MemoryUpdate.model_json_schema()),
        "limits": limits,
        "contentSchemas": shapes,
        "instruction": "No memory changes were applied. Use the selected content type's fields. "
        "Notes accept text and optional plan (an existing plan ID), not sources. "
        "Beliefs require claim and uncertainty; citations are optional historical references. "
        "Each source list accepts at most 20 citations: select relevant support for this revision instead of appending old citations. Earlier revisions retain their evidence. "
        "After chat_read, observation:communication is a usable source. Game state need not be refreshed for this error.",
    }


class MemoryRead(BoundaryModel):
    ids: Annotated[list[Source], Field(max_length=20)]
    includeRetired: bool = False


class MemorySearch(BoundaryModel):
    type: Literal["soul", "plan", "belief", "note", "contribution", "decision", "observation", "statement"] | None = (
        None
    )
    subject: str = Field(default="", max_length=160)
    state: Literal["active", "retired", "all"] = "active"
    query: str = Field(default="", max_length=200)
    history: bool = False
    id: str = Field(default="", max_length=160)
    offset: NonnegativeInt = 0


class Intent(BoundaryModel):
    reason: str = Field(default="", max_length=1000)
    plan: str = Field(default="", max_length=160)
    expectedOutcome: str = Field(default="", max_length=1000)
    assumptions: Annotated[list[Assumption], Field(max_length=20)] = Field(default_factory=list)

    @model_validator(mode="after")
    def has_reason_or_plan(self):
        if not self.reason.strip() and not self.plan:
            raise ValueError("Supply a reason or a plan reference")
        return self


REQUIRED_COMMANDS = {
    "SET_RESEARCH_QUEUE",
    "CHOOSE_FREE_TECHNOLOGY",
    "ADOPT_POLICY",
    "CHOOSE_FREE_GREAT_PERSON",
    "FOUND_CITY",
    "QUEUE_CONSTRUCTION",
    "REMOVE_CONSTRUCTION",
    "CLEAR_CONSTRUCTION_QUEUE",
    "REORDER_CONSTRUCTION",
    "BUY_CONSTRUCTION",
    "BUY_TILE",
    "SELL_BUILDING",
    "UPGRADE_UNIT",
    "DISBAND_UNIT",
    "ACTIVATE_GREAT_PERSON",
    "ADD_SPACESHIP_PART",
    "SET_UNIT_DESTINATION",
    "REBASE_UNIT",
    "PARADROP",
    "AIR_SWEEP",
    "NUCLEAR_STRIKE",
    "CREATE_WATER_IMPROVEMENT",
    "CAST_DIPLOMATIC_VOTE",
    "ABSTAIN_DIPLOMATIC_VOTE",
    "DECLARE_WAR",
    "CITY_STATE_ACTION",
    "GIFT_UNIT",
    "ATTACK",
    "CITY_ATTACK",
    "PROMOTE_UNIT",
    "RESOLVE_CAPTURED_CITY",
    "MANAGE_CAPTURED_CITY",
    "RESPOND_FRIENDSHIP",
    "MOVE_CONSTRUCTION",
}
REQUIRED_TOOLS = {"trade_offer", "trade_decide", "friendship_offer", "commitment_offer", "commitment_decide"}


def public_record(key, record):
    body = {k: v for k, v in record["body"].items() if not k.startswith("_")}
    if record["category"] not in MEMORY_TYPES:
        body = model_view(body)
    return {
        "id": key,
        "type": record["category"],
        **(body if record["category"] in MEMORY_TYPES | {"decision"} else {"body": body}),
        **(
            {"reviewSources": sorted({ref["key"] for ref in record["body"]["_review"]})}
            if record["category"] in MEMORY_TYPES and record["body"].get("_review")
            else {}
        ),
    }


def record_text(body):
    return body.get("objective") or body.get("claim") or body.get("text") or body.get("reason", "")


class RecordMetadata(BoundaryModel):
    id: str
    createdTurn: NonnegativeInt | None
    updatedTurn: NonnegativeInt | None
    state: Literal["active", "retired"]
    needsReview: list[str] = Field(default_factory=list)
    reviewSources: list[str] = Field(default_factory=list)
    retirementReason: Retirement | None = None
    assessment: str = ""
    evidence: str | None = None
    historical: bool = False
    basis: JsonObject | None = None
    unverifiedSources: list[str] = Field(default_factory=list)


class StoredPlan(Plan, RecordMetadata):
    pass


class StoredBelief(Belief, RecordMetadata):
    pass


class StoredNote(Note, RecordMetadata):
    pass


class StoredSoul(Soul, RecordMetadata):
    pass


class EvidenceRecord(BoundaryModel):
    id: str
    type: Literal["observation", "statement"]
    body: JsonObject
    evidence: str | None = None
    historical: bool = False


class ContributionRecord(BoundaryModel):
    id: str
    type: Literal["contribution"]
    body: JsonObject
    evidence: str | None = None
    historical: bool = False


class DecisionRecord(BoundaryModel):
    id: str
    type: Literal["decision"]
    turn: NonnegativeInt | None
    intent: Intent | None
    command: JsonObject
    outcome: Literal["pending", "confirmed", "rejected", "uncertain"]
    receipt: JsonObject | None
    subjects: list[str] = Field(default_factory=list)
    settlement: JsonObject | None = None
    evidence: str | None = None
    historical: bool = False


ReadRecord = Annotated[
    StoredPlan | StoredBelief | StoredNote | StoredSoul | EvidenceRecord | DecisionRecord | ContributionRecord,
    Field(discriminator="type"),
]


class MemoryReadResult(BoundaryModel):
    records: Annotated[list[ReadRecord], Field(max_length=20)]


class MemorySearchResult(MemoryReadResult):
    hasMore: bool
    nextOffset: NonnegativeInt


class ChangeResult(BoundaryModel):
    id: str | None
    accepted: bool
    reason: str | None = None
    recovery: JsonObject | None = None
    recoverable: bool | None = None
    invalidFields: list[JsonObject] | None = None
    limits: list[JsonObject] | None = None
    instruction: str | None = None
    warnings: list[JsonObject] = Field(default_factory=list)
    contribution: bool = False
    version: int | None = None
    record: ReadRecord | None = None


class MemoryUpdateResult(BoundaryModel):
    changes: Annotated[list[ChangeResult], Field(max_length=20)]
    error: str | None = None
    recoverable: bool | None = None
    diagnosticId: str | None = None
    instruction: str | None = None


class SummaryAction(BoundaryModel):
    decision: str
    action: str
    reason: str
    outcome: Literal["pending", "confirmed", "rejected", "uncertain"]
    command: JsonObject
    receipt: JsonObject | None = None
    settlement: JsonObject | None = None


class MemoryAssessment(BoundaryModel):
    id: str
    type: Literal["plan", "belief", "soul", "note"]
    text: str
    state: Literal["active", "retired"]
    retirementReason: Retirement | None = None
    assessment: str = ""


class TurnSummary(BoundaryModel):
    turn: NonnegativeInt | None
    actions: list[SummaryAction] = Field(default_factory=list)
    observations: list[EvidenceRecord] = Field(default_factory=list)
    memoryChanges: list[MemoryAssessment] = Field(default_factory=list)
    unresolved: list[str] = Field(default_factory=list)


class HistoryReadResult(BoundaryModel):
    turn: NonnegativeInt
    actions: Annotated[list[SummaryAction], Field(max_length=20)]
    hasMore: bool
    nextOffset: NonnegativeInt
