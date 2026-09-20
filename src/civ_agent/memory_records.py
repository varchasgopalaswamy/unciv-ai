"""Validated agent-authored memory and intent. Operational metadata is service owned."""

from typing import Annotated, Literal

from pydantic import Field, model_validator

from civ_agent.protocol import JsonObject
from civ_agent.schemas import BoundaryModel, NonnegativeInt

Text = Annotated[str, Field(min_length=1, max_length=2000)]
Source = Annotated[str, Field(min_length=1, max_length=160)]
Sources = Annotated[list[Source], Field(max_length=20)]
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
    sources: Annotated[list[Source], Field(min_length=1, max_length=20)]
    contradictingSources: Sources = Field(default_factory=list)


class Note(Content):
    type: Literal["note"]
    text: Text
    plan: str = Field(default="", max_length=160)


class Soul(Content):
    type: Literal["soul"]
    text: Text


RecordContent = Annotated[Plan | Belief | Note | Soul, Field(discriminator="type")]


class Create(BoundaryModel):
    op: Literal["create"]
    content: RecordContent


class Revise(BoundaryModel):
    op: Literal["revise", "replace"]
    id: Source
    content: RecordContent


class Retire(BoundaryModel):
    op: Literal["retire"]
    id: Source
    reason: Retirement
    assessment: str = Field(default="", max_length=2000)


class MemoryUpdate(BoundaryModel):
    changes: Annotated[list[Annotated[Create | Revise | Retire, Field(discriminator="op")]], Field(max_length=20)]


class MemoryRead(BoundaryModel):
    ids: Annotated[list[Source], Field(max_length=20)]
    includeRetired: bool = False


class MemorySearch(BoundaryModel):
    type: Literal["soul", "plan", "belief", "note", "decision", "observation", "statement"] | None = None
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
            raise ValueError("Supply a reason or an active plan")
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
    return {
        "id": key,
        "type": record["category"],
        **(body if record["category"] in MEMORY_TYPES | {"decision"} else {"body": body}),
    }


def record_text(body):
    return body.get("objective") or body.get("claim") or body.get("text") or body.get("reason", "")


class RecordMetadata(BoundaryModel):
    id: str
    createdTurn: NonnegativeInt | None
    updatedTurn: NonnegativeInt | None
    state: Literal["active", "retired"]
    needsReview: list[str] = Field(default_factory=list)
    retirementReason: Retirement | None = None
    assessment: str = ""
    successor: str | None = None
    evidence: str | None = None
    historical: bool = False


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
    StoredPlan | StoredBelief | StoredNote | StoredSoul | EvidenceRecord | DecisionRecord, Field(discriminator="type")
]


class MemoryReadResult(BoundaryModel):
    records: Annotated[list[ReadRecord], Field(max_length=20)]


class MemorySearchResult(MemoryReadResult):
    hasMore: bool
    nextOffset: NonnegativeInt


class ChangeResult(BoundaryModel):
    id: str | None
    accepted: bool
    retired: str | None = None
    reason: str | None = None


class MemoryUpdateResult(BoundaryModel):
    changes: Annotated[list[ChangeResult], Field(max_length=20)]


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
