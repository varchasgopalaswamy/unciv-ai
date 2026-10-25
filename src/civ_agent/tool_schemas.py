"""One definition for each Python tool's advertised schema and input validation."""

from typing import Annotated, Literal

from pydantic import BaseModel, Field, RootModel, field_validator, model_validator
from pydantic.json_schema import SkipJsonSchema

from civ_agent.delivery_options import DeliveryOptions
from civ_agent.economy_roles import CityOverride, EconomyRequest, FinishRole, RequestDisposition
from civ_agent.game_commands import Position
from civ_agent.inspection_bundle import InspectionBundle
from civ_agent.memory_records import (
    EconomyMemoryUpdate,
    HistoryReadResult,
    Intent,
    MemoryContributions,
    MemoryRead,
    MemoryReadResult,
    MemorySearch,
    MemorySearchResult,
    MemoryUpdate,
    MemoryUpdateResult,
)
from civ_agent.observations import DETAIL_MODELS, DetailResult, EntityDetails
from civ_agent.schemas import BoundaryModel, NonnegativeInt
from civ_agent.war_state import EscortAssignment, MilitaryOperation, RoleMessage, WarGoals

ShortReference = Annotated[str, Field(min_length=1, max_length=16, pattern=r"^[A-Za-z0-9_-]+$")]
PlayerName = Annotated[str, Field(min_length=1, max_length=128, pattern=r"\S")]


class PlayerTurn(DeliveryOptions):
    view: Literal["current", "full", "economy"] = Field(
        default="current",
        description="Keep the current view (default), request the full overview, or select a focused economy brief. All owned details remain readable in every view.",
    )
    cities: Annotated[list[Annotated[str, Field(min_length=1, max_length=128)]], Field(max_length=500)] | None = Field(
        default=None,
        description="Optional owned city handles to include in this overview. Empire constraints and the owned-city index remain visible. Omit for all cities; details of any owned city remain available.",
    )


class SpecialistFinishRole(BoundaryModel):
    requests: list[RequestDisposition] = Field(default_factory=list, max_length=100)


class EconomyWait(BoundaryModel):
    requestIds: Annotated[
        list[Annotated[str, Field(min_length=1, max_length=128)]],
        Field(max_length=100, json_schema_extra={"uniqueItems": True}),
    ] = Field(default_factory=list)

    @field_validator("requestIds")
    @classmethod
    def unique_requests(cls, value):
        if len(value) != len(set(value)):
            raise ValueError("List each request ID only once")
        return value


class CommandHelp(BoundaryModel):
    kinds: Annotated[list[Annotated[str, Field(min_length=1, max_length=128)]], Field(min_length=1, max_length=16)]


class ReviewPlayerDetails(DeliveryOptions):
    section: Literal["economy", "diplomacy", "research", "policies", "map_index", "settlement"]


class MovementPreview(EntityDetails):
    destination: Position


class MissionPreview(MovementPreview):
    mission: Literal["REBASE", "PARADROP", "AIR_SWEEP", "NUCLEAR_STRIKE"]


class PlayerInspection(BoundaryModel):
    subject: Literal[
        "POLITICS", "WONDERS", "NOTIFICATIONS", "REPLAY", "CIVILOPEDIA", "TILE", "UNIT", "ROAD", "CITY_FOCUS"
    ]
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
    intent: Intent | None = None


class Partner(BoundaryModel):
    partner: str


class Proposal(BoundaryModel):
    proposalId: ShortReference = Field(description="Copy the short proposal ID from a received offer or trade.")


class Agreement(BoundaryModel):
    agreement: str


class TradeItem(BoundaryModel):
    type: Annotated[str, Field(min_length=1, max_length=64, description="Copy type from trade_options.")]
    name: str
    amount: Annotated[int, Field(ge=1)]
    duration: int


class TradeDraft(Partner):
    give: Annotated[list[TradeItem], Field(max_length=64)]
    receive: Annotated[list[TradeItem], Field(max_length=64)]
    fundResearch: bool = True


class TradeOffer(Partner, WithIntent):
    give: Annotated[list[TradeItem], Field(max_length=64)]
    receive: Annotated[list[TradeItem], Field(max_length=64)]
    # Defaults make these fields optional without accepting explicit JSON null.
    # exclude_unset preserves omission when forwarding arguments to the host.
    counterofferTo: ShortReference = Field(
        default="", description="Existing trade ID to counter, after trade_read. Omit when creating a new offer."
    )


class TradeDecision(Proposal, WithIntent):
    decision: Literal["accept", "decline", "withdraw"]


class HistoryRead(BoundaryModel):
    turn: NonnegativeInt
    offset: NonnegativeInt = 0


class ChatHistory(BoundaryModel):
    with_player: str = Field(default="", alias="with")
    query: str = ""


class ChatRead(BoundaryModel):
    handledMessages: Annotated[list[Annotated[str, Field(min_length=1)]], Field(max_length=50)] = Field(
        default_factory=list
    )
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
    delivery: Literal["auto", "immediate"] = "auto"
    text: Annotated[str, Field(min_length=1, max_length=4000, pattern=r"\S")]
    to: PlayerName = ""
    recipients: Annotated[list[PlayerName], Field(min_length=1, max_length=100)] = Field(
        default_factory=list,
        description="Private recipients, each once, excluding yourself. Omit to and nativeAction. Reply to all using the message participants minus yourself.",
        json_schema_extra={"uniqueItems": True},
    )
    mentions: Annotated[list[PlayerName], Field(max_length=100)] = Field(default_factory=list)
    intent: Intent | None = None

    @field_validator("mentions", "recipients")
    @classmethod
    def unique_mentions(cls, value):
        if len(set(value)) != len(value):
            raise ValueError("List each player only once")
        return value

    @model_validator(mode="after")
    def exclusive_addressing(self):
        if "recipients" in self.model_fields_set and {"to", "nativeAction"} & self.model_fields_set:
            raise ValueError("Use recipients without to or nativeAction")
        return self


class GameplayChatSend(ChatSend):
    nativeAction: (
        Annotated[NativeDenouncement | NativeDemand | NativeDiplomaticResponse, Field(discriminator="kind")] | None
    ) = None


class ChatDelivery(BoundaryModel):
    deliveryId: str
    action: Literal["status", "immediate", "cancel"] = "status"


class NativeAdvice(BoundaryModel):
    cities: Annotated[list[str], Field(max_length=100)] = Field(default_factory=list)
    units: Annotated[list[str], Field(max_length=100)] = Field(default_factory=list)


class CommitmentOffer(WithIntent):
    participants: Annotated[list[PlayerName], Field(min_length=2, max_length=100)] = Field(
        description="All participants, including your own civilization. Use names from chatRecipients."
    )
    terms: Annotated[str, Field(min_length=1, max_length=4000, pattern=r"\S")]
    conditions: Annotated[str, Field(min_length=1, max_length=2000, pattern=r"\S")]
    kind: Literal["peace", "other"]
    expiresTurn: Annotated[int, Field(ge=0, le=2147483647)] = 0
    sources: Annotated[list[ShortReference], Field(max_length=100)] = Field(
        default_factory=list, description="Optional message IDs visible to every participant; copy IDs, not citations."
    )
    reviseProposalId: ShortReference = Field(
        default="", description="Your existing commitment ID to revise. Omit when creating a new commitment."
    )

    @field_validator("sources")
    @classmethod
    def unique_sources(cls, value):
        if len(set(value)) != len(value):
            raise ValueError("List each message ID only once")
        return value


class AcceptCommitment(Proposal, WithIntent):
    decision: Literal["accept"]


class ChangeCommitment(Proposal, WithIntent):
    decision: Literal["withdraw", "reconsider"]
    publicReason: Annotated[str, Field(min_length=1, max_length=1000, pattern=r"\S")] = Field(
        description="Explanation shared with the participants. intent.reason is private."
    )


class CommitmentDecision(RootModel[Annotated[AcceptCommitment | ChangeCommitment, Field(discriminator="decision")]]):
    model_config = {"json_schema_extra": {"type": "object"}}


class FriendshipOffer(Partner, WithIntent):
    pass


TOOL_MODELS: dict[str, type[BaseModel]] = {
    "player_turn": PlayerTurn,
    "inspect_bundle": InspectionBundle,
    "command_help": CommandHelp,
    "war_goals": WarGoals,
    "escort_assignment": EscortAssignment,
    "military_operation": MilitaryOperation,
    "role_message": RoleMessage,
    "finish_role": FinishRole,
    "economy_wait": EconomyWait,
    "economy_request": EconomyRequest,
    "city_override": CityOverride,
    **DETAIL_MODELS,
    "movement_preview": MovementPreview,
    "player_inspect": PlayerInspection,
    "mission_preview": MissionPreview,
    "trade_options": Partner,
    "trade_prepare": TradeDraft,
    "trade_read": Proposal,
    "agreement_list": BoundaryModel,
    "agreement_read": Agreement,
    "trade_offer": TradeOffer,
    "friendship_offer": FriendshipOffer,
    "trade_decide": TradeDecision,
    "history_read": HistoryRead,
    "chat_read": ChatRead,
    "chat_history": ChatHistory,
    "chat_send": GameplayChatSend,
    "chat_delivery": ChatDelivery,
    "native_advice": NativeAdvice,
    "commitment_offer": CommitmentOffer,
    "commitment_decide": CommitmentDecision,
    "memory_read": MemoryRead,
    "memory_search": MemorySearch,
    "memory_update": MemoryUpdate,
}


def tool_model(name: str, purpose: str) -> type[BaseModel]:
    if name == "finish_role" and purpose in {"economy", "military"}:
        return SpecialistFinishRole
    if name == "player_details" and purpose == "review":
        return ReviewPlayerDetails
    if name == "memory_update":
        if purpose == "economy":
            return EconomyMemoryUpdate
        if purpose not in {"gameplay"}:
            return MemoryContributions
    return ChatSend if name == "chat_send" and purpose != "gameplay" else TOOL_MODELS[name]


TOOL_OUTPUT_MODELS: dict[str, type[BoundaryModel]] = {
    **dict.fromkeys(DETAIL_MODELS, DetailResult),
    "history_read": HistoryReadResult,
    "memory_read": MemoryReadResult,
    "memory_search": MemorySearchResult,
    "memory_update": MemoryUpdateResult,
}
