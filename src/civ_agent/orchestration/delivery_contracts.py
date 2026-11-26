"""Outgoing domain identities and native activity boundaries; no dispatch journal."""

import hashlib
from typing import Annotated, Literal

from pydantic import AliasChoices, Field, ValidationError, model_validator

from civ_agent.host_effects import PreparedHostEffect
from civ_agent.orchestration.contracts import Digest, Reference, Scope
from civ_agent.protocol import JsonObject
from civ_agent.schemas import BoundaryModel, Identifier, NonnegativeInt

PENDING_NATIVE_DELIVERY_QUERY = "pending_native_delivery_v1"


class OutgoingDraft(BoundaryModel):
    version: Literal[1] = 1
    scope: Scope
    delivery_id: Identifier
    immediate: bool = False


class OutgoingState(BoundaryModel):
    result: Reference
    disposition: str
    deadline: Annotated[float, Field(ge=0, allow_inf_nan=False)]
    immediate: bool = False
    held: bool = False
    terminal: bool = False


class OutgoingDelivery(BoundaryModel):
    request: OutgoingDraft
    deadline: Annotated[float, Field(ge=0, allow_inf_nan=False)]
    force: bool = False
    ordered: bool = True


def delivery_request_id(epoch: str, delivery_id: str) -> str:
    return "r" + hashlib.sha256((epoch + ":" + delivery_id).encode()).hexdigest()[:24]


class FrozenNativeDelivery(BoundaryModel):
    operation: OutgoingDelivery
    job: Identifier
    prepared: PreparedHostEffect
    seal: Digest

    @model_validator(mode="after")
    def binding(self):
        request, prepared = self.operation.request, self.prepared
        identifier = delivery_request_id(request.scope.epoch, request.delivery_id)
        payload = prepared.payload or {}
        if (
            self.job != "dispatch-" + identifier
            or prepared.name != "chat_send"
            or prepared.transport != "player_communication"
            or prepared.result is not None
            or prepared.routed
            or payload.get("op") != "send"
            or payload.get("epoch") != request.scope.epoch
            or payload.get("requestId") != identifier
            or payload.get("nativeAction") is not None
            or not isinstance(payload.get("text"), str)
        ):
            raise ValueError("OUTGOING_NATIVE_BINDING_CONFLICT")
        return self


class NativeDeliveryPreparation(BoundaryModel):
    state: OutgoingState
    effect: FrozenNativeDelivery | None = None


class DeliveryAdmission(BoundaryModel):
    task_id: Digest = Field(validation_alias=AliasChoices("task_id", "workflowId"))
    run_id: Identifier = Field(validation_alias=AliasChoices("run_id", "runId"))

    @property
    def workflowId(self):
        return self.task_id

    @property
    def runId(self):
        return self.run_id


class NativeDeliveryExecution(BoundaryModel):
    receipt: JsonObject | None = None
    state: OutgoingState | None = None

    @model_validator(mode="after")
    def outcome(self):
        if (self.receipt is None) == (self.state is None):
            raise ValueError("OUTGOING_EXECUTION_INVALID")
        return self


class NativeDeliveryApplication(BoundaryModel):
    effect: FrozenNativeDelivery
    receipt: JsonObject


class NativeDeliveryMessage(BoundaryModel):
    message: Identifier
    author: str
    participants: list[str]
    public: bool
    text: str
    mentions: list[str] = Field(default_factory=list)
    turn: NonnegativeInt
    nativeAction: None = None


def validate_native_delivery_receipt(payload, receipt, *, author):
    """Validate one authoritative native response against its exact original send."""
    from civ_agent.civilization_state import receipt_is_uncertain
    from civ_agent.orchestration.store import JournalConflict

    if (
        not isinstance(receipt, dict)
        or receipt_is_uncertain(receipt)
        or receipt.get("queued")
        or receipt.get("uncertain")
    ):
        raise JournalConflict("DELIVERY_RECEIPT_UNCERTAIN")
    if "error" in receipt:
        if not isinstance(receipt["error"], str) or receipt["error"] == "REQUEST_ID_CONFLICT":
            raise JournalConflict("DELIVERY_RECEIPT_UNCERTAIN")
        return
    try:
        message = NativeDeliveryMessage.model_validate(receipt.get("message"))
    except ValidationError:
        raise JournalConflict("OUTGOING_MESSAGE_BINDING_CONFLICT") from None
    recipients = payload.get("recipients") or ([payload["to"]] if payload.get("to") else [])
    if (
        message.author != author
        or message.text != payload["text"]
        or message.mentions != payload.get("mentions", [])
        or message.public is not (not recipients)
        or recipients
        and sorted(message.participants) != sorted({author, *recipients})
    ):
        raise JournalConflict("OUTGOING_MESSAGE_BINDING_CONFLICT")
