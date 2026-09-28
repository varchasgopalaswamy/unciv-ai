"""Shared types at the JSON and process boundaries.

JSON objects remain open where the upstream protocol can add fields. Stable
objects produced by this package have explicit field types.
"""

from collections.abc import Awaitable, Callable
from typing import Any, Protocol, TypedDict

type JsonObject = dict[str, Any]
type Notification = Callable[[str, JsonObject], Awaitable[None]]


class EventPage(TypedDict):
    events: list[JsonObject]
    nextCursor: int
    hasMore: bool
    available: bool


class AgentProcess(Protocol):
    async def start(self, token: str, model: str | None) -> None: ...

    async def request(self, method: str, parameters: JsonObject, /) -> JsonObject: ...

    async def close(self) -> None: ...
