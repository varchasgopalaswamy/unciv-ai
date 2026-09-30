"""One definition for each Python tool's advertised schema and input validation."""

from typing import Annotated, Literal

from pydantic import Field, field_validator
from pydantic.json_schema import SkipJsonSchema

from civ_agent.game_commands import Position
from civ_agent.memory_records import (
    HistoryReadResult,
    Intent,
    MemoryRead,
    MemoryReadResult,
    MemorySearch,
    MemorySearchResult,
    MemoryUpdate,
    MemoryUpdateResult,
)
from civ_agent.observations import DETAIL_MODELS, DetailResult, EntityDetails
from civ_agent.schemas import BoundaryModel, NonnegativeInt


class MovementPreview(EntityDetails):
    destination: Position


class MissionPreview(MovementPreview):
    mission: Literal["REBASE", "PARADROP", "AIR_SWEEP", "NUCLEAR_STRIKE"]


class PlayerInspection(BoundaryModel):
    subject: Literal["POLITICS", "WONDERS", "NOTIFICATIONS", "REPLAY", "CIVILOPEDIA", "TILE", "UNIT", "ROAD"]
    target: Annotated[str, Field(min_length=1, max_length=128)] = ""
    destination: Position | SkipJsonSchema[None] = None
    category: Annotated[str, Field(min_length=1, max_length=128)] = ""
    names: Annotated[list[Annotated[str, Field(min_length=1, max_length=128)]], Field(max_length=16)] = Field(
        default_factory=list
    )
    search: Annotated[str, Field(max_length=128)] = ""
    turn: NonnegativeInt | SkipJsonSchema[None] = None
    offset: NonnegativeInt = 0
    limit: Annotated[int, Field(ge=1, le=100)] = 50

    @field_validator("destination", "turn")
    @classmethod
    def supplied_values_are_not_null(cls, value):
        if value is None:
            raise ValueError("Omit unused arguments instead of passing null")
        return value


class WithIntent(BoundaryModel):
    intent: Intent


class Partner(BoundaryModel):
    partner: str


class Proposal(BoundaryModel):
    proposal: str


class Agreement(BoundaryModel):
    agreement: str


class TradeItem(BoundaryModel):
    type: Literal[
        "Gold",
        "Gold_Per_Turn",
        "Luxury_Resource",
        "Strategic_Resource",
        "Treaty",
        "Agreement",
        "Embassy",
        "City",
        "WarDeclaration",
        "PeaceProposal",
    ]
    name: str
    amount: Annotated[int, Field(ge=1)]
    duration: int


class TradeOffer(Partner, WithIntent):
    give: Annotated[list[TradeItem], Field(max_length=64)]
    receive: Annotated[list[TradeItem], Field(max_length=64)]
    # Defaults make these fields optional without accepting explicit JSON null.
    # exclude_unset preserves omission when forwarding arguments to the host.
    proposal: str = ""


class TradeDecision(Proposal, WithIntent):
    decision: Literal["accept", "decline", "withdraw"]


class HistoryRead(BoundaryModel):
    turn: NonnegativeInt
    offset: NonnegativeInt = 0


class ChatHistory(BoundaryModel):
    with_player: str = Field(default="", alias="with")
    query: str = ""


class ChatRead(BoundaryModel):
    includeContext: bool = False
    includeHistory: bool = False


class NativeDenouncement(BoundaryModel):
    kind: Literal["DENOUNCE"]
    target: str


class NativeDemand(BoundaryModel):
    kind: Literal["ISSUE_DEMAND"]
    target: str
    selection: Literal["DoNotSettleNearUs", "DoNotSpreadReligion", "DontSpyOnUs"]


class NativeDiplomaticResponse(BoundaryModel):
    kind: Literal["RESPOND_DIPLOMACY"]
    target: str
    selection: str


class ChatSend(BoundaryModel):
    text: str
    to: str = ""
    mentions: list[str] = Field(default_factory=list)
    intent: Intent | None = None
    nativeAction: (
        Annotated[NativeDenouncement | NativeDemand | NativeDiplomaticResponse, Field(discriminator="kind")] | None
    ) = None


class CommitmentOffer(WithIntent):
    participants: list[str]
    terms: str
    conditions: str
    kind: Literal["peace", "other"]
    expiresTurn: int = 0
    sources: list[str] = Field(default_factory=list)
    proposal: str = ""


class CommitmentDecision(Proposal, WithIntent):
    decision: Literal["accept", "withdraw", "reconsider"]
    reason: str = ""


class FriendshipOffer(Partner, WithIntent):
    pass


TOOL_MODELS: dict[str, type[BoundaryModel]] = {
    **DETAIL_MODELS,
    "movement_preview": MovementPreview,
    "player_inspect": PlayerInspection,
    "mission_preview": MissionPreview,
    "trade_options": Partner,
    "trade_read": Proposal,
    "agreement_list": BoundaryModel,
    "agreement_read": Agreement,
    "trade_offer": TradeOffer,
    "friendship_offer": FriendshipOffer,
    "trade_decide": TradeDecision,
    "history_read": HistoryRead,
    "chat_read": ChatRead,
    "chat_history": ChatHistory,
    "chat_send": ChatSend,
    "commitment_offer": CommitmentOffer,
    "commitment_decide": CommitmentDecision,
    "memory_read": MemoryRead,
    "memory_search": MemorySearch,
    "memory_update": MemoryUpdate,
}

TOOL_OUTPUT_MODELS: dict[str, type[BoundaryModel]] = {
    **dict.fromkeys(DETAIL_MODELS, DetailResult),
    "history_read": HistoryReadResult,
    "memory_read": MemoryReadResult,
    "memory_search": MemorySearchResult,
    "memory_update": MemoryUpdateResult,
}
