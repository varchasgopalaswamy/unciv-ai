"""Trusted coordinator handoff inputs for exact native receipt settlement."""

from typing import Annotated

from pydantic import Field, model_validator

from civ_agent.host_effects import PreparedHostEffect, host_request_id
from civ_agent.orchestration.contracts import Digest, GatewayJob, HandoffResult, TurnTicket
from civ_agent.protocol import JsonObject
from civ_agent.schemas import BoundaryModel

PENDING_NATIVE_HANDOFF_QUERY = "pending_native_handoff_v1"


class NativeHandoffRequest(BoundaryModel):
    ticket: TurnTicket
    attempt: Annotated[int, Field(ge=0)]

    @property
    def call_id(self):
        return "native:handoff:" + str(self.attempt)


class FrozenNativeHandoff(BoundaryModel):
    request: NativeHandoffRequest
    main: GatewayJob
    prepared: PreparedHostEffect
    seal: Digest

    @model_validator(mode="after")
    def binding(self):
        ticket, prepared = self.request.ticket, self.prepared
        payload = prepared.payload or {}
        if (
            (self.main.scope.game, self.main.scope.player, self.main.scope.epoch)
            != (ticket.scope.game, ticket.scope.player, ticket.scope.epoch)
            or self.main.purpose != "gameplay"
            or prepared.name != "player_command"
            or prepared.transport != "player_command"
            or prepared.result is not None
            or prepared.routed
            or payload.get("kind") != "END_TURN"
            or payload.get("epoch") != ticket.scope.epoch
            or payload.get("requestId") != host_request_id(self.main.job, self.request.call_id)
            or not payload.get("revision")
            or not payload.get("reviewId")
        ):
            raise ValueError("NATIVE_HANDOFF_BINDING_CONFLICT")
        return self


class NativeHandoffPreparation(BoundaryModel):
    effect: FrozenNativeHandoff | None = None
    result: HandoffResult | None = None

    @model_validator(mode="after")
    def outcome(self):
        if (self.effect is None) == (self.result is None):
            raise ValueError("NATIVE_HANDOFF_PREPARATION_INVALID")
        return self


class NativeHandoffApplication(BoundaryModel):
    effect: FrozenNativeHandoff
    receipt: JsonObject


def confirmed_handoff(effect: FrozenNativeHandoff, receipt: JsonObject) -> bool:
    payload = effect.prepared.payload or {}
    return "error" not in receipt and all(
        receipt.get(key) == payload.get(key) for key in ("requestId", "epoch", "kind")
    )
