"""Model-facing recovery data; never authorizes actions or replays effects."""

import json
from contextvars import ContextVar
from typing import Literal

from pydantic import Field
from pydantic_ai import ToolFailed

from civ_agent.protocol import JsonObject
from civ_agent.schemas import BoundaryModel


class RecoveryError(BoundaryModel):
    code: str
    message: str
    tool: str | None = Field(default=None, exclude_if=lambda value: value is None)


class ToolRecovery(BoundaryModel):
    status: Literal["needs_recovery"] = "needs_recovery"
    error: RecoveryError
    recovery: JsonObject


recoveries: ContextVar[list[JsonObject] | None] = ContextVar("tool_recoveries", default=None)


def recovery_response(code: str, message: str, *, tool: str | None = None, details: JsonObject | None = None):
    details = dict(details or {})
    if not details.get("instruction"):
        details["instruction"] = (
            "Correct the failed call using this feedback. Inspect confirmed effects before continuing; never replay them."
        )
    return ToolRecovery(
        error=RecoveryError(code=code, message=message, tool=tool),
        recovery={
            "reads": [],
            **details,
        },
    ).model_dump(mode="json")


class RecoverableToolError(ToolFailed):
    """Keep typed diagnostics across the sandbox's exception boundary."""

    def __init__(self, response: JsonObject):
        self.response = response
        if (pending := recoveries.get()) is not None:
            pending.append(response)
        super().__init__(json.dumps(response, ensure_ascii=False))
