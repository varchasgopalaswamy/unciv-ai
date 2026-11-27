"""Small durable controller state; game mutations remain native-authoritative."""

import asyncio
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Literal

from filelock import FileLock
from pydantic import BaseModel, ConfigDict, Field

from civ_agent.execution_policy import ExecutionPolicy, legacy_execution_policy
from civ_agent.gameplay_policy import GameplayPolicy, legacy_gameplay_policy
from civ_agent.service_io import ServiceIO
from civ_agent.tls import private_directory, regular_file, write_private_json


class LocalRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", validate_default=True)


class RoleStatus(LocalRecord):
    purpose: str
    job: str
    phase: str
    started_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    phase_started_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    model: str | None = None
    model_requests: int = 0
    sdk_requests: int | None = None
    usage: dict[str, int] = Field(default_factory=dict)
    context_usage: dict[str, int | float | bool] = Field(default_factory=dict)
    native_job: dict | None = None
    run_id: str | None = None
    released: bool = False
    permit_state: str | None = None
    pending_report: dict | None = None
    pending_release: bool = False


class AttemptRecord(LocalRecord):
    gameplay_policy: GameplayPolicy = Field(
        default_factory=legacy_gameplay_policy, exclude_if=lambda value: value.version == 0
    )
    execution_policy: ExecutionPolicy = Field(
        default_factory=legacy_execution_policy, exclude_if=lambda value: value.version == 0
    )
    id: Annotated[str, Field(pattern=r"^[A-Za-z0-9_-]{1,128}$")]
    player: str
    civilization: str
    epoch: str
    turn: int
    handoff: int
    status: Literal["PREPARING", "RUNNING", "RECOVERING", "INTERRUPTED", "COMPLETED"] = "PREPARING"
    roles: list[RoleStatus] = Field(default_factory=list)
    ticket: dict | None = None
    handoff_effect: dict | None = None
    handoff_receipt: dict | None = None


class CommunicationDeferral(LocalRecord):
    version: Literal[1] = 1
    status: Literal["UNAVAILABLE"] = "UNAVAILABLE"
    job: str
    next_handoff: Annotated[int, Field(ge=0)]
    notices: list[dict] = Field(default_factory=list)
    reason: str


class ControllerState(LocalRecord):
    schemaVersion: Literal[1] = 1
    runtime: Literal["pydantic-local-v1"] = "pydantic-local-v1"
    game: str
    epoch: str
    lease_id: str
    config_sha256: str
    initial_started_at: datetime
    deadline: datetime
    initial_turn: int
    mode: Literal["RUNNING", "PAUSED", "STOPPED", "FINISHED", "FAILED"] = "RUNNING"
    phase: str = "PREPARING"
    phase_started_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    reason: str | None = None
    turn: int = 0
    handoff: int = 0
    model_invocations: int = 0
    model_requests: int = 0
    sdk_requests: int | None = None
    sdk_requests_observed: int = 0
    active_civilization: str | None = None
    active_controller: str | None = None
    active_attempt: AttemptRecord | None = None
    attempts: Annotated[int, Field(ge=0)] = 0
    resume_authorization: str | None = None
    seats: list[dict] = Field(default_factory=list)
    native_jobs: dict[str, RoleStatus] = Field(default_factory=dict)
    recent_roles: list[RoleStatus] = Field(default_factory=list)
    communication_deferrals: dict[str, CommunicationDeferral] = Field(default_factory=dict)
    limits: dict = Field(default_factory=dict)
    completed_attempts: list[AttemptRecord] = Field(default_factory=list)
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


def desktop_turn_progress(state):
    """Publish only coarse progress for the current AI turn."""
    attempt = state["active_attempt"]
    if (
        state["mode"] != "RUNNING"
        or state["active_controller"] != "AGENT"
        or attempt is None
        or any(attempt[key] != state[key] for key in ("epoch", "turn", "handoff"))
        or attempt["civilization"] != state["active_civilization"]
    ):
        return None
    stage = {
        "PREPARING": "PREPARING",
        "WAITING_REVIEW": "REVIEWING",
        "ROLE_WORK": "PLAYING",
        "MAIN_FOLLOWUP": "PLAYING",
        "HANDOFF": "FINISHING",
    }.get(state["phase"])
    if stage == "PLAYING":
        phases = {
            role["phase"]
            for role in attempt["roles"]
            if role["purpose"] in {"gameplay", "economy", "military"}
            and role["phase"] not in {"ROLE_FINISHED", "COMPLETED", "FAILED", "INTERRUPTED", "WAITING_ROLE"}
        }
        if phases == {"WAITING_FOR_MODEL"}:
            stage = "WAITING_FOR_MODEL"
    if stage is None:
        return None
    return {"epoch": state["epoch"], "turn": state["turn"], "handoff": state["handoff"], "stage": stage}


class ControllerStore:
    def __init__(self, root: Path):
        self.root = private_directory(root / "native-runtime")
        self.path = self.root / "controller.json"
        self.status_path = self.root / "status.json"
        self.desktop_progress_path = self.root / "desktop-progress.json"
        self.lock = FileLock(self.root / "controller.lock", timeout=0)
        self.state: ControllerState | None = None
        self._write_lock = asyncio.Lock()
        self.io = ServiceIO()
        self._published_phase: str | None = None

    def load(self) -> ControllerState | None:
        if self.path.exists() or self.path.is_symlink():
            self.state = ControllerState.model_validate_json(regular_file(self.path, private=True).read_bytes())
            self._published_phase = self.state.phase
            # Status is a derived view, including after interrupted publication.
            self._status(self.state.model_dump(mode="json"))
        return self.state

    @property
    def current(self) -> ControllerState:
        if self.state is None:
            raise RuntimeError("LOCAL_CONTROLLER_STATE_REQUIRED")
        return self.state

    async def save(self, update: Callable[[ControllerState], None] | None = None):
        async with self._write_lock:
            if self.state is None:
                raise RuntimeError("LOCAL_CONTROLLER_STATE_REQUIRED")
            if update is not None:
                update(self.state)
            if self._published_phase is not None and self.state.phase != self._published_phase:
                self.state.phase_started_at = datetime.now(UTC)
            self.state.updated_at = datetime.now(UTC)
            value = self.state.model_dump(mode="json")
            ControllerState.model_validate(value)
            # Retain serialization ownership until every accepted write settles,
            # even when the caller receives repeated cancellation.
            await self.io.call(self._publish, value)
            self._published_phase = self.state.phase

    def _status(self, value):
        status = {**value, "roles": value["active_attempt"]["roles"] if value["active_attempt"] else []}
        write_private_json(self.status_path, status)

    def _publish(self, value):
        for attempt in value["completed_attempts"]:
            directory = private_directory(self.root / "attempts")
            write_private_json(directory / (attempt["id"] + ".json"), attempt)
        write_private_json(self.path, value)
        self._status(value)
        progress = desktop_turn_progress(value)
        if progress is None:
            self.desktop_progress_path.unlink(missing_ok=True)
        else:
            write_private_json(self.desktop_progress_path, progress)

    async def close(self):
        await self.io.close()

    async def phase(self, phase: str, *, reason: str | None = None):
        def update(state):
            state.phase, state.reason = phase, reason

        await self.save(update)

    async def role(self, role: RoleStatus):
        def update(state):
            previous = next((item for item in state.native_jobs.values() if item.job == role.job), None)
            if previous is None and state.active_attempt is not None:
                previous = next((item for item in state.active_attempt.roles if item.job == role.job), None)
            if previous is not None:
                role.started_at = previous.started_at
                if role.run_id is None:
                    role.run_id = previous.run_id
                if previous.phase == role.phase:
                    role.phase_started_at = previous.phase_started_at
            if role.run_id is not None:
                state.recent_roles = [r for r in state.recent_roles if r.run_id != role.run_id][-31:] + [role]
            if role.native_job is not None:
                native = role.native_job
                key = native["scope"]["player"] + ":" + native["job"]
                # Completed history belongs in the native ledger and attempt
                # archive. Only outstanding permits need restart inventory.
                if role.released:
                    state.native_jobs.pop(key, None)
                else:
                    state.native_jobs[key] = role
            if state.active_attempt is None:
                return
            if role.native_job is not None and role.native_job["scope"]["player"] != state.active_attempt.player:
                return
            state.active_attempt.roles = [r for r in state.active_attempt.roles if r.job != role.job] + [role]

        await self.save(update)

    async def archive_attempt(self, attempt: AttemptRecord):
        async with self._write_lock:
            directory = private_directory(self.root / "attempts")
            await self.io.call(write_private_json, directory / (attempt.id + ".json"), attempt.model_dump(mode="json"))
            self.current.completed_attempts = [*self.current.completed_attempts[-7:], attempt]
