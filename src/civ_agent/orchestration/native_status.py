"""Read-only native host results and independently named workflow projections."""

import os
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Annotated, Literal

from pydantic import BaseModel, Field, model_validator
from sqlalchemy import inspect, select
from sqlalchemy.exc import SQLAlchemyError

from civ_agent.container_support import ContainerError
from civ_agent.monitoring import path_for, read_database, records
from civ_agent.orchestration.contracts import OpaqueId
from civ_agent.orchestration.job_contracts import ApiCounts, Count, MemoryEditCounts
from civ_agent.orchestration.managed_epochs import published_managed_epoch
from civ_agent.orchestration.managed_game_contracts import (
    ManagedGameRequest,
    ManagedGameResult,
    ManagedGameStatus,
)
from civ_agent.orchestration.managed_host import validate_limit_increase
from civ_agent.schemas import BoundaryModel

type HostLabel = Annotated[str, Field(min_length=1, max_length=256)]
type FiniteNumber = Annotated[float, Field(allow_inf_nan=False)]


class HostUsage(BoundaryModel):
    input_tokens: Count
    cached_input_tokens: Count
    output_tokens: Count
    reasoning_output_tokens: Count
    total_tokens: Count
    complete: bool

    @model_validator(mode="after")
    def total(self):
        if self.total_tokens != self.input_tokens + self.output_tokens:
            raise ValueError("HOST_USAGE_TOTAL_CONFLICT")
        return self


class HostScore(BoundaryModel):
    civilization: HostLabel
    controller: Literal["AGENT", "HUMAN", "BUILT_IN_AI"]
    defeated: bool
    score: int
    rawScore: FiniteNumber
    breakdown: Annotated[dict[HostLabel, FiniteNumber], Field(max_length=128)]


class HostVictory(BoundaryModel):
    epoch: OpaqueId
    recordedAtMs: Count
    winner: HostLabel | None
    victoryType: HostLabel | None
    victoryTurn: Count | None
    scores: Annotated[list[HostScore], Field(max_length=128)]


class HostPlayerUsage(BoundaryModel):
    civilization: HostLabel
    usage: HostUsage
    modelJobs: Count
    modelRequests: Count
    gameplayUsage: HostUsage
    economyUsage: HostUsage
    militaryUsage: HostUsage
    consultationUsage: HostUsage
    reviewUsage: HostUsage
    chatUsage: HostUsage
    agentCalls: Annotated[dict[HostLabel, ApiCounts], Field(max_length=512)]
    memoryEdits: MemoryEditCounts | None
    hostCalls: Annotated[dict[HostLabel, ApiCounts], Field(max_length=512)]
    activeMs: Count
    jobElapsedMs: Count


class HostTiming(BoundaryModel):
    calls: Count
    elapsedMs: Count
    maxMs: Count


class HostGameResult(BoundaryModel):
    """Payload-free schema published by Kotlin's AccountingMonitoring."""

    schemaVersion: Literal[1]
    matchId: OpaqueId
    epoch: OpaqueId
    status: Literal["running", "incomplete", "completed"]
    result: HostVictory | None
    scores: Annotated[list[HostScore], Field(max_length=128)]
    startedAtMs: Count
    elapsedMs: Count
    hostRevision: HostLabel
    engineRevision: HostLabel
    seed: int
    ruleset: HostLabel
    speed: HostLabel
    difficulty: HostLabel
    victoryTypes: Annotated[list[HostLabel], Field(max_length=128)]
    maxTurns: Count
    usage: HostUsage
    modelJobs: Count
    modelRequests: Count
    agentCalls: Annotated[dict[HostLabel, ApiCounts], Field(max_length=512)]
    memoryEdits: MemoryEditCounts | None
    hostCalls: Annotated[dict[HostLabel, ApiCounts], Field(max_length=512)]
    players: Annotated[list[HostPlayerUsage], Field(max_length=128)]
    hostTimings: Annotated[dict[HostLabel, HostTiming], Field(max_length=512)]

    @model_validator(mode="after")
    def terminal_epoch(self):
        if self.result is not None and self.result.epoch != self.epoch:
            raise ValueError("HOST_RESULT_EPOCH_CONFLICT")
        if (self.status == "completed") != (self.result is not None):
            raise ValueError("HOST_RESULT_STATUS_CONFLICT")
        return self


@dataclass(frozen=True)
class NativeGameProjection:
    workflow_status: ManagedGameStatus | None
    workflow_result: ManagedGameResult | None
    workflow_result_updated_at: float | None
    result: HostGameResult | None
    result_updated_at: float | None


def _read[Model: BaseModel](path: Path, model: type[Model]) -> tuple[Model | None, float | None]:
    try:
        with path.open("rb") as stream:
            updated_at = os.fstat(stream.fileno()).st_mtime
            raw = stream.read()
    except FileNotFoundError:
        return None, None
    try:
        return model.model_validate_json(raw), updated_at
    except ValueError:
        raise ContainerError(f"Invalid native runtime projection: {path}") from None


def host_result(root: Path, *, epoch: str | None) -> tuple[HostGameResult | None, float | None]:
    """Read only the maintained native host publication, never scheduler state."""
    path = path_for(root)
    if not path.exists():
        return None, None
    try:
        with read_database(path) as db:
            tables = inspect(db).get_table_names()
            if not tables:
                return None, None
            if records.name not in tables:
                raise ContainerError(f"Unsupported native host result publication: {path}")
            row = (
                db.execute(
                    select(records.c.payload, records.c.epoch, records.c.updated_ms).where(
                        records.c.collection == "state", records.c.key == "result"
                    )
                )
                .mappings()
                .one_or_none()
            )
    except SQLAlchemyError:
        raise ContainerError(f"Cannot read native host result publication: {path}") from None
    if row is None:
        return None, None
    try:
        result = HostGameResult.model_validate_json(row["payload"])
    except ValueError:
        raise ContainerError(f"Invalid native host result publication: {path}") from None
    if epoch is None or row["epoch"] != epoch or result.epoch != epoch:
        raise ContainerError(f"Native host result epoch conflicts with frozen game authority: {path}")
    updated_at = row["updated_ms"]
    if type(updated_at) is not int or updated_at < 0:
        raise ContainerError(f"Invalid native host result publication timestamp: {path}")
    return result, updated_at / 1000


def native_projection(root: Path) -> NativeGameProjection:
    """Missing publications are pending; invalid publications require inspection."""
    directory = root / "native-runtime"
    request = None
    if any(path.exists() or path.is_symlink() for path in (directory / "game.json", directory / "current-epoch.json")):
        try:
            request = published_managed_epoch(root)
        except (OSError, ValueError):
            raise ContainerError(
                f"Invalid native runtime projection (epoch publication): {directory / 'game.json'}"
            ) from None
    status, _ = _read(directory / "status.json", ManagedGameStatus)
    result, updated_at = _read(directory / "result.json", ManagedGameResult)
    values = [value for value in (status, result) if value is not None]
    identities = {(value.game, value.epoch) for value in values}
    if request is not None:
        identities.add((request.authority.game, request.authority.epoch))
        try:
            for value in values:
                validate_limit_increase(request.authority.limits, value.effective_limits or request.authority.limits)
            if status is not None and result is not None:
                validate_limit_increase(
                    status.effective_limits or request.authority.limits,
                    result.effective_limits or request.authority.limits,
                )
        except ValueError:
            raise ContainerError(f"Native runtime limits conflict with frozen game authority: {directory}") from None
        if any(
            value.deadline
            != value.initial_started_at
            + timedelta(seconds=(value.effective_limits or request.authority.limits).max_seconds)
            for value in values
        ):
            raise ContainerError(f"Native runtime deadline conflicts with frozen game authority: {directory}")
        try:
            for value in values:
                validate_epoch_publication(request, value)
        except ValueError:
            raise ContainerError(f"Native runtime checkpoint provenance conflicts: {directory}") from None
    if len(identities) > 1:
        raise ContainerError(f"Native runtime identity conflicts with frozen game authority: {directory}")
    if (
        status is not None
        and result is not None
        and (
            status.initial_started_at,
            status.initial_turn,
        )
        != (result.initial_started_at, result.initial_turn)
    ):
        raise ContainerError(f"Native runtime initial position conflicts: {directory}")
    epoch = request.authority.epoch if request is not None else next(iter(identities))[1] if identities else None
    host, host_updated_at = host_result(root, epoch=epoch)
    return NativeGameProjection(
        workflow_status=status,
        workflow_result=result,
        workflow_result_updated_at=updated_at,
        result=host,
        result_updated_at=host_updated_at,
    )


def validate_epoch_publication(request: ManagedGameRequest, value: ManagedGameStatus | ManagedGameResult) -> None:
    """A replacement epoch retains its proved original budget and counter floor."""
    state = request.continuation
    if state is None:
        return
    validate_limit_increase(
        state.effective_limits or request.authority.limits, value.effective_limits or request.authority.limits
    )
    handoff = value.handoff if isinstance(value, ManagedGameStatus) else value.handoffs
    if (
        (value.initial_started_at, value.initial_turn) != (state.initial_started_at, state.initial_turn)
        or value.model_invocations < state.model_invocations
        or value.turn < state.host.turn
        or handoff < state.host.handoff
    ):
        raise ValueError("MANAGED_EPOCH_PUBLICATION_BUDGET_CONFLICT")
