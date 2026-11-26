"""Prepared native requests; the host remains the action-receipt authority."""

import hashlib
from dataclasses import dataclass, field
from typing import Literal

from pydantic import ConfigDict, with_config

from civ_agent.protocol import JsonObject

HOST_EFFECT_TOOLS = frozenset(
    {
        "player_command",
        "trade_offer",
        "trade_decide",
        "friendship_offer",
        "chat_send",
        "commitment_offer",
        "commitment_decide",
    }
)


def host_request_id(job: str, call_id: str) -> str:
    return "r" + hashlib.sha256((job + ":" + call_id).encode()).hexdigest()[:24]


@with_config(ConfigDict(strict=False))
@dataclass(frozen=True)
class PreparedHostEffect:
    name: str
    arguments: JsonObject
    transport: Literal["player_command", "player_communication"] | None = None
    payload: JsonObject | None = None
    intent: JsonObject | None = None
    initiative_decisions: list[JsonObject] = field(default_factory=list)
    result: JsonObject | None = None
    routed: bool = False
