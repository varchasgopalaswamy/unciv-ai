"""Private frozen command boundaries; the native host owns action receipts."""

from typing import Annotated

from pydantic import Field, model_validator

from civ_agent.host_effects import PreparedHostEffect
from civ_agent.orchestration.contracts import Digest, GatewayCall, GatewayJob
from civ_agent.orchestration.gateway_state import GatewayState
from civ_agent.protocol import JsonObject
from civ_agent.schemas import BoundaryModel

PENDING_NATIVE_COMMANDS_QUERY = "pending_native_commands_v1"


class NativeCommandStep(BoundaryModel):
    identity: GatewayJob
    call: GatewayCall
    index: Annotated[int, Field(ge=0, le=11)]
    # An upstream SDK identity, preserved verbatim; it is not an owned Scope ID.
    run_id: Annotated[str, Field(min_length=1, max_length=1024)] | None = None


class FrozenNativeCommand(BoundaryModel):
    step: NativeCommandStep
    prepared: PreparedHostEffect
    state: GatewayState
    sources: dict[str, JsonObject] = Field(default_factory=dict)
    seal: Digest


class PendingNativeCommandBatch(BoundaryModel):
    """Original prepared envelopes still owned by one native workflow batch."""

    identity: GatewayJob
    call: GatewayCall = Field(repr=False)
    effects: Annotated[list[FrozenNativeCommand], Field(max_length=12, repr=False)] = Field(default_factory=list)

    @model_validator(mode="after")
    def binding(self):
        indexes = [effect.step.index for effect in self.effects]
        if indexes != sorted(set(indexes)) or any(
            effect.step.identity != self.identity or effect.step.call != self.call for effect in self.effects
        ):
            raise ValueError("NATIVE_COMMAND_EXPOSURE_CONFLICT")
        return self


class NativeCommandPreparation(BoundaryModel):
    effect: FrozenNativeCommand | None = None
    result: JsonObject | None = None
    stop: str | None = None
    feedback: JsonObject | None = None

    @model_validator(mode="after")
    def outcome(self):
        if self.effect is not None and self.result is not None:
            raise ValueError("A frozen effect cannot also have an early result")
        if self.effect is None and self.result is None and self.stop is None:
            raise ValueError("A frozen effect, early result or stop reason is required")
        return self


class NativeCommandApplication(BoundaryModel):
    effect: FrozenNativeCommand
    receipt: JsonObject


class NativeCommandApplied(BoundaryModel):
    result: JsonObject
    stop: str | None = None


class NativeCommandFinish(BoundaryModel):
    identity: GatewayJob
    call: GatewayCall
