"""Versioned status contract shared by CLI publication and repair policy."""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict


class RuntimeStatus(BaseModel):
    model_config = ConfigDict(extra="allow")
    game: str | None = None
    mode: Literal["RUNNING", "PAUSED", "STOPPED", "FINISHED", "FAILED"]
    phase: str = "PREPARING"
    reason: str | None = None
    deadline: datetime | None = None
    active_controller: str | None = None
    epoch: str | None = None
    turn: int = 0
    handoff: int = 0
    model_invocations: int = 0
    model_requests: int = 0


class GameStatusEnvelope(BaseModel):
    model_config = ConfigDict(extra="allow")
    version: Literal[1] = 1
    runtime_status: RuntimeStatus | None = None
    containers: dict = {}
    result: dict | None = None


class RepairOwner(BaseModel):
    version: Literal[1] = 1
    game: str
    monitor_pid: int
    directory: str
    status: Literal["DISPATCHING", "COMPLETED"] = "DISPATCHING"
    tokens: int | None = None
