"""Frozen SDK approval metadata for an exact, authenticated peace prerequisite."""

from typing import Annotated, Any, Literal

from pydantic import Field, model_validator

from civ_agent.orchestration.contracts import Digest, GatewayCall, GatewayJob, OpaqueId, PeaceRequest, digest
from civ_agent.orchestration.store import JournalConflict
from civ_agent.protocol import JsonObject
from civ_agent.schemas import BoundaryModel

type PeaceTool = Literal["act", "execute_commands"]
SdkRunId = Annotated[str, Field(min_length=1, max_length=1024)]


class PeaceApprovalWait(BoundaryModel):
    identity: GatewayJob
    tool: PeaceTool
    call: GatewayCall
    run_id: SdkRunId
    requests: Annotated[list[PeaceRequest], Field(min_length=1, max_length=12)]

    @model_validator(mode="after")
    def binding(self):
        if self.call.decision.get("kind") != "act" or not self.call.decision.get("operations"):
            raise ValueError("PEACE_ACTION_BATCH_REQUIRED")
        if len({request.basis for request in self.requests}) != len(self.requests):
            raise ValueError("PEACE_REQUEST_DUPLICATED")
        owner = self.identity.scope
        if any(
            not request.managed
            or (request.scope.game, request.scope.player, request.scope.epoch)
            != (owner.game, owner.player, owner.epoch)
            for request in self.requests
        ):
            raise ValueError("PEACE_APPROVAL_SCOPE_CONFLICT")
        return self

    @property
    def id(self) -> str:
        return digest(self.model_dump(mode="json"))


class PeacePreflightRequest(BoundaryModel):
    identity: GatewayJob
    tool: PeaceTool
    call_id: OpaqueId
    model_step: Annotated[int, Field(ge=0)]
    run_id: SdkRunId
    arguments: JsonObject
    approval: PeaceApprovalWait | None = None


class PeacePreflightReply(BoundaryModel):
    wait: PeaceApprovalWait | None = None
    result: JsonObject | None = None

    @model_validator(mode="after")
    def exclusive(self):
        if self.wait is not None and self.result is not None:
            raise ValueError("PEACE_PREFLIGHT_CONFLICT")
        return self


class PeaceReady(BoundaryModel):
    wait_id: Digest


def peace_action_decision(tool: PeaceTool, arguments: JsonObject) -> JsonObject:
    """Use the same action envelope for preflight and SDK approval validation."""
    if tool == "act":
        operations = arguments.get("operations", [])
    else:
        operations = []
        for supplied in arguments.get("commands", []):
            body = dict(supplied)
            metadata = {key: body.pop(key) for key in ("intent", "reviewAfter") if key in body}
            operations.append({"tool": "player_command", "arguments": {"command": body, **metadata}})
    return {"kind": "act", "operations": operations}


def approved_peace_wait(
    identity: GatewayJob,
    call_id: str | None,
    tool: PeaceTool,
    decision: JsonObject,
    *,
    approved: bool,
    metadata: Any,
) -> PeaceApprovalWait | None:
    """Only trusted native approval metadata may restore the original call."""
    from civ_agent.orchestration.gateway_execution import native_call_id

    if not approved or not isinstance(metadata, dict) or "nativePeace" not in metadata:
        return None
    wait = PeaceApprovalWait.model_validate(metadata["nativePeace"])
    if (wait.identity, wait.tool, wait.call.call_id, wait.call.decision) != (
        identity,
        tool,
        native_call_id(call_id) if call_id is not None else None,
        decision,
    ):
        raise JournalConflict("PEACE_APPROVAL_CALL_CONFLICT")
    return wait
