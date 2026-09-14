"""One definition for each Python tool's advertised schema and input validation."""

from typing import Annotated, Literal

from pydantic import Field

from civ_agent.protocol import JsonObject
from civ_agent.schemas import BoundaryModel, NonnegativeInt


class Partner(BoundaryModel):
    partner: str


class Proposal(BoundaryModel):
    proposal: str


class TradeItem(BoundaryModel):
    type: Literal[
        "Gold", "Gold_Per_Turn", "Luxury_Resource", "Strategic_Resource", "Treaty", "Agreement", "Embassy", "City"
    ]
    name: str
    amount: Annotated[int, Field(ge=1)]
    duration: int


class TradeOffer(Partner):
    give: Annotated[list[TradeItem], Field(max_length=64)]
    receive: Annotated[list[TradeItem], Field(max_length=64)]
    # Defaults make these fields optional without accepting explicit JSON null.
    # exclude_unset preserves omission when forwarding arguments to the host.
    proposal: str = ""


class TradeDecision(Proposal):
    decision: Literal["accept", "decline", "withdraw"]


class HistoryRead(BoundaryModel):
    turn: NonnegativeInt
    offset: NonnegativeInt = 0


class ChatHistory(BoundaryModel):
    with_player: str = Field(default="", alias="with")
    query: str = ""


class ChatSend(BoundaryModel):
    text: str
    to: str = ""
    mentions: list[str] = Field(default_factory=list)


class CommitmentOffer(BoundaryModel):
    participants: list[str]
    terms: str
    conditions: str
    kind: Literal["peace", "other"]
    expiresTurn: int = 0
    sources: list[str] = Field(default_factory=list)
    proposal: str = ""


class CommitmentDecision(Proposal):
    decision: Literal["accept", "withdraw", "reconsider"]
    reason: str = ""


class MemoryRead(BoundaryModel):
    keys: Annotated[list[str], Field(max_length=20)]


class MemoryChange(BoundaryModel):
    key: str
    category: Literal["policy", "plan", "belief"]
    body: JsonObject


class MemoryUpdate(BoundaryModel):
    changes: Annotated[list[MemoryChange], Field(max_length=20)]


TOOL_MODELS: dict[str, type[BoundaryModel]] = {
    "trade_options": Partner,
    "trade_read": Proposal,
    "trade_offer": TradeOffer,
    "friendship_offer": Partner,
    "trade_decide": TradeDecision,
    "history_read": HistoryRead,
    "chat_read": BoundaryModel,
    "chat_history": ChatHistory,
    "chat_send": ChatSend,
    "commitment_offer": CommitmentOffer,
    "commitment_decide": CommitmentDecision,
    "memory_read": MemoryRead,
    "memory_update": MemoryUpdate,
}
