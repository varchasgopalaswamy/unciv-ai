"""Validated agent-authored memory and intent. Operational metadata is service owned."""

from typing import Annotated, Literal

from pydantic import Field, ValidationError

from civ_agent.model_view import model_view
from civ_agent.protocol import JsonObject
from civ_agent.schemas import BoundaryModel, NonnegativeInt, argument_feedback, argument_shape

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
BRIEF_TYPES = {"strategy", "handoff"}
MEMORY_TYPES = BRIEF_TYPES | {"belief", "note"}


class Assumption(BoundaryModel):
    text: Text
    source: str = Field(default="", max_length=160)


class Content(BoundaryModel):
    subjects: Annotated[
        list[Source],
        Field(
            max_length=20,
            description="At most 20 relevant subject tags for this revision; lists replace existing tags.",
        ),
    ] = Field(default_factory=list)
    priority: Annotated[int, Field(ge=1, le=5)] = 3
    expiresTurn: NonnegativeInt | None = None


ShortThought = Annotated[str, Field(min_length=1, max_length=400)]
Thoughts = Annotated[list[ShortThought], Field(max_length=5)]


class Strategy(Content):
    type: Literal["strategy"]
    assessment: Text
    reasons: Thoughts = Field(default_factory=list)
    uncertainties: Thoughts = Field(default_factory=list)
    reconsiderWhen: Thoughts = Field(default_factory=list)
    assumptions: Annotated[list[Assumption], Field(max_length=20)] = Field(default_factory=list)
    sources: Sources = Field(default_factory=list)


class Handoff(Content):
    type: Literal["handoff"]
    text: Text = Field(
        description="Unfinished work, unresolved questions or useful context absent from current observations. "
        "Omit routine state and instructions to preserve an existing plan; provisional strategy belongs in strategy."
    )
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
    relatedMemory: str = Field(
        default="",
        max_length=160,
        description="Optional historical memory ID, not prose. Unread references are explicitly unverified.",
    )


RecordContent = Annotated[Strategy | Handoff | Belief | Note, Field(discriminator="type")]
CONTENT_MODELS = {"strategy": Strategy, "handoff": Handoff, "belief": Belief, "note": Note}


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
            description="Only changed content fields of a belief or note you read. Strategy and handoff require replace. Omitted fields are retained. Lists replace the old list; they never append. Record type, IDs and dates cannot be changed.",
        ),
    ]


class Replace(BoundaryModel):
    op: Literal["replace"]
    id: Source
    content: Annotated[Strategy | Handoff, Field(discriminator="type")]


class MemoryUpdate(BoundaryModel):
    changes: Annotated[
        list[Annotated[Create | Retire | Amend | Replace, Field(discriminator="op")]], Field(max_length=20)
    ]


class MemoryContributions(BoundaryModel):
    """Communication may append contributions, never revise the shared notebook."""

    changes: Annotated[list[Create], Field(max_length=20)]


class MemoryContentError(ValueError):
    def __init__(self, details: JsonObject):
        super().__init__("INVALID_ARGUMENT")
        self.details = details


def amended_content(previous: JsonObject, fields: JsonObject) -> JsonObject:
    """Validate the complete revision before any writes, including retained fields."""
    if previous["type"] in BRIEF_TYPES:
        raise ValueError("REPLACEMENT_REQUIRED: replace the complete strategy or handoff content")
    model = CONTENT_MODELS[previous["type"]]
    if "type" in fields:
        raise ValueError("TYPE_CHANGE_REQUIRES_NEW_RECORD")
    content = {**{key: previous[key] for key in model.model_fields if key in previous}, **fields}
    try:
        return model.model_validate(content).model_dump()
    except ValidationError as error:
        feedback = argument_feedback(error, model.model_json_schema(), ["fields"])
        feedback["instruction"] = (
            feedback["instruction"] + " Amend only content fields. Lists replace existing lists. "
            "No part of this edit was applied; other edits have their own acceptance results."
        ).strip()
        raise MemoryContentError(feedback) from None


def memory_argument_error(arguments: JsonObject, error: ValidationError) -> JsonObject:
    shapes = {}
    changes = arguments.get("changes")
    for change in changes if isinstance(changes, list) else []:
        content = change.get("content") if isinstance(change, dict) else None
        kind = content.get("type") if isinstance(content, dict) else None
        if isinstance(kind, str) and kind in CONTENT_MODELS:
            schema = CONTENT_MODELS[kind].model_json_schema()
            shapes[kind] = argument_shape(schema, schema.get("$defs", {}))
    feedback = argument_feedback(error, MemoryUpdate.model_json_schema())
    return {
        **feedback,
        "error": "INVALID_ARGUMENT",
        "applied": False,
        "recoverable": True,
        "contentSchemas": shapes,
        "instruction": feedback["instruction"]
        + " No memory changes were applied. Correct the listed fields using contentSchemas. "
        "Lists replace existing lists; earlier revisions retain their contents. "
        "Game state need not be refreshed for this error.",
    }


class MemoryRead(BoundaryModel):
    ids: Annotated[list[Source], Field(max_length=20)]
    includeRetired: bool = False


class MemorySearch(BoundaryModel):
    type: (
        Literal["strategy", "handoff", "belief", "note", "contribution", "decision", "observation", "statement"] | None
    ) = None
    subject: str = Field(default="", max_length=160)
    state: Literal["active", "retired", "all"] = "active"
    query: str = Field(default="", max_length=200)
    history: bool = False
    id: str = Field(default="", max_length=160)
    offset: NonnegativeInt = 0


class InitiativeDecision(BoundaryModel):
    initiative: Annotated[int, Field(ge=1, le=2)] = Field(description="One-based initiative number in that review.")
    decision: Literal["acted", "deferred", "rejected"]
    reason: Annotated[str, Field(min_length=1, max_length=600, pattern=r"\S")]


class RecordedInitiativeDecision(InitiativeDecision):
    reviewTurn: NonnegativeInt


class Intent(BoundaryModel):
    reason: Annotated[str, Field(min_length=1, max_length=1000, pattern=r"\S")]
    strategy: str = Field(default="", max_length=160)
    expectedOutcome: str = Field(default="", max_length=1000)
    assumptions: Annotated[list[Assumption], Field(max_length=20)] = Field(default_factory=list)


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
    body = {k: v for k, v in record["body"].items() if not k.startswith("_") and k != "basis"}
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
    return body.get("assessment") or body.get("claim") or body.get("text") or body.get("reason", "")


class RecordMetadata(BoundaryModel):
    id: str
    createdTurn: NonnegativeInt | None
    updatedTurn: NonnegativeInt | None
    state: Literal["active", "retired"]
    needsReview: list[str] = Field(default_factory=list)
    reviewSources: list[str] = Field(default_factory=list)
    retirementReason: Retirement | None = None
    retirementAssessment: str = ""
    evidence: str | None = None
    historical: bool = False
    unverifiedSources: list[str] = Field(default_factory=list)


class StoredStrategy(Strategy, RecordMetadata):
    pass


class StoredBelief(Belief, RecordMetadata):
    pass


class StoredNote(Note, RecordMetadata):
    pass


class StoredHandoff(Handoff, RecordMetadata):
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
    initiativeDecisions: list[RecordedInitiativeDecision] = Field(
        default_factory=list, exclude_if=lambda value: not value
    )
    command: JsonObject
    outcome: Literal["pending", "confirmed", "rejected", "uncertain"]
    receipt: JsonObject | None
    subjects: list[str] = Field(default_factory=list)
    settlement: JsonObject | None = None
    evidence: str | None = None
    historical: bool = False


ReadRecord = Annotated[
    StoredStrategy | StoredBelief | StoredNote | StoredHandoff | EvidenceRecord | DecisionRecord | ContributionRecord,
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
    initiativeDecisions: Annotated[list[RecordedInitiativeDecision], Field(max_length=2)] = Field(
        default_factory=list, exclude_if=lambda value: not value
    )


class MemoryAssessment(BoundaryModel):
    id: str
    type: Literal["strategy", "handoff", "belief", "note"]
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
